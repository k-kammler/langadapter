"""A CMake project's build recorded and replayed, through the real cmake and
GCC: its own test program and a utest.h suite."""
from __future__ import annotations

import os
import shutil

import pytest

import langadapter as la
from langadapter.runs import BenchmarkRequest, SuiteRunRequest

from langadapter.c import _build
from conftest import needs_gcc, needs_mull

pytestmark = [needs_gcc, pytest.mark.skipif(shutil.which("cmake") is None,
                                            reason="needs cmake on PATH")]

TESTS = """\
#include "utest.h"
#include "calc.h"

UTEST(clamp, to_the_limit) {
    ASSERT_EQ(10L, clamp_to_limit(50));
}

UTEST_MAIN()
"""


def _logging_ar(tool_dir, log):
    """A directory holding an ar that logs each call before it runs the real one."""
    tool_dir.mkdir()
    ar = tool_dir / "ar"
    ar.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{shutil.which("ar")}" "$@"\n',
                  encoding="utf-8")
    ar.chmod(0o755)
    return tool_dir


def _stdbuf_only(directory):
    """The new ``directory`` with stdbuf, which every run of utest.h tests
    needs, and no other tool."""
    directory.mkdir()
    (directory / "stdbuf").symlink_to(shutil.which("stdbuf"))
    return directory


def _with_path(root, path, **settings):
    """A C adapter whose context's PATH is ``path``."""
    from langadapter.c.adapter import CAdapter

    environment = {**CAdapter.context_environment(settings), "PATH": path}
    return CAdapter(la.ProjectContext(root=str(root), environment=environment,
                                      interpreter=la.Interpreter(path=shutil.which("gcc"))))


def test_cmake_and_ar_come_from_the_contexts_path(project, suite, tmp_path):
    """The context's PATH, not this process's, says where cmake and ar are:
    without cmake on it the project misses setup, and its ar archives the
    target's library."""
    root = project("calc_cmake")
    run = _with_path(root, str(_stdbuf_only(tmp_path / "empty")), c_target="src/calc.c").run_suite(
        SuiteRunRequest(suite(TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.MISSING_SETUP, run.reason
    assert "needs cmake on PATH" in run.reason
    log = tmp_path / "ar.log"
    tools = _logging_ar(tmp_path / "tools", log)
    run = _with_path(root, f"{tools}{os.pathsep}{os.environ['PATH']}",
                     c_target="src/calc.c").run_suite(SuiteRunRequest(suite(TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert "libcalc.a" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("taskset") is None, reason="needs taskset")
def test_a_context_without_path_has_its_tools_looked_for_in_os_defpath(
        project, suite, tmp_path, monkeypatch):
    """Without PATH in the context, its compiler, cmake and a timing's
    taskset are looked for where its commands are, in os.defpath, not on
    this process's PATH."""
    from langadapter.c.adapter import CAdapter

    monkeypatch.setattr(os, "defpath", str(_stdbuf_only(tmp_path / "defpath")))
    root = project("calc_cmake")
    compiler = CAdapter.discover_runtime(str(root), {}, environment={}).interpreter
    c = CAdapter(la.ProjectContext(
        root=str(root), interpreter=la.Interpreter(path=shutil.which("gcc")),
        environment=la.Environment(CAdapter.context_environment({"c_target": "src/calc.c"}),
                                   inherit=False)))
    run = c.run_suite(SuiteRunRequest(suite(TESTS), timeout_s=120))
    timed = c.tests.benchmark(BenchmarkRequest(
        suite(TESTS), runs=1, timeout_s=120, cpus=str(min(os.sched_getaffinity(0)))))
    assert (compiler, run.outcome, "needs cmake on PATH" in (run.reason or ""), timed.outcome,
            "needs taskset" in (timed.reason or "")) == (
        None, la.Outcome.MISSING_SETUP, True, la.Outcome.UNSUPPORTED, True), (run.reason,
                                                                               timed.reason)


def _files(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.is_file())


def test_a_cmake_project_is_discovered_and_built_outside_its_tree(project, adapter, suite):
    root = project("calc_cmake")
    before = _files(root)
    assert _build.discover_kind(root) == "cmake"
    # The target needs calc_config.h, which the configure step writes.
    run = adapter(root, c_target="src/calc.c").run_suite(
        SuiteRunRequest(suite(TESTS), timeout_s=300, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert {5, 8, 9} <= set(run.coverage.covered) and 11 not in run.coverage.covered
    assert _files(root) == before


def test_a_cmake_projects_own_program_is_its_baseline(project, adapter):
    root = project("calc_cmake")
    run = adapter(root, c_tests="programs", c_target="src/calc.c").run_suite(
        SuiteRunRequest(la.Suite(str(root), ("test/test-calc.c",)), timeout_s=300,
                        coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert [t.test.text for t in run.tests] == ["test/test-calc.c::main"]
    assert {5, 6, 11} <= set(run.coverage.covered)


def test_cmake_comes_before_make_unless_the_setting_names_a_kind(project, adapter, suite):
    """A project with a CMakeLists.txt and a Makefile builds with cmake, the
    earlier kind in the discovery order, and with make when c_build_kind
    names it. Each build defines BUILT_BY its own way, and the test file
    sees it through the target's shared flags."""
    root = project("calc_make")
    makefile = root / "Makefile"
    makefile.write_text(makefile.read_text(encoding="utf-8").replace(
        "CPPFLAGS = -Iinclude\n", "CPPFLAGS = -Iinclude -DBUILT_BY=1\n"), encoding="utf-8")
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(calc C)\n"
        "add_compile_definitions(BUILT_BY=2)\ninclude_directories(include)\n"
        "add_executable(app src/main.c src/calc.c src/util.c)\n"
        "target_link_libraries(app m)\n", encoding="utf-8")

    def built_by(expected, **settings):
        text = ('#include "utest.h"\n#include "calc.h"\n\n'
                f"UTEST(build, kind) {{\n    ASSERT_EQ({expected}, BUILT_BY);\n}}\n\n"
                "UTEST_MAIN()\n")
        return adapter(root, c_target="src/calc.c", **settings).run_suite(
            SuiteRunRequest(suite(text), timeout_s=300))

    discovered = built_by(2)
    assert discovered.outcome is la.Outcome.COMPLETED, discovered.reason_text(None)
    chosen = built_by(1, c_build_kind="make")
    assert chosen.outcome is la.Outcome.COMPLETED, chosen.reason_text(None)


def test_cmakes_options_reach_its_configure_step(project, adapter, suite):
    root = project("calc_cmake")
    twenty = TESTS.replace("ASSERT_EQ(10L", "ASSERT_EQ(20L")
    run = adapter(root, c_target="src/calc.c", c_build="-DCALC_LIMIT=20").run_suite(
        SuiteRunRequest(suite(twenty), timeout_s=300))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    default = adapter(root, c_target="src/calc.c").run_suite(
        SuiteRunRequest(suite(twenty), timeout_s=300))
    assert default.outcome is la.Outcome.TEST_FAILURES


CTEST = dict(c_tests="ctest", c_target="src/calc.c")


def _ctest_suite(root):
    return la.Suite(str(root), ("CMakeLists.txt",))


def test_ctests_tests_run_as_ctest_runs_them(project, adapter):
    root = project("calc_cmake")
    run = adapter(root, **CTEST).run_suite(SuiteRunRequest(
        _ctest_suite(root), timeout_s=300, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    verdicts = {t.test.qualname: t.outcome for t in run.tests}
    # WILL_FAIL inverts a failing program, and ENVIRONMENT reaches the test.
    assert verdicts == {"own_program": "passed", "low": "passed", "high": "passed",
                        "wrong_on_purpose": "passed", "from_environment": "passed",
                        "a_script": "skipped"}
    [script] = [t for t in run.tests if t.test.qualname == "a_script"]
    assert "no link of the build's record wrote" in script.message
    assert {t.test.text for t in run.tests} >= {"CMakeLists.txt::low"}
    assert {5, 6, 8, 9, 11} <= set(run.coverage.covered) and 15 not in run.coverage.covered


def test_ctests_tests_are_timed_as_ctest_runs_them(project, adapter):
    """Each repetition runs each test ctest lists as a suite run does, with
    its arguments and environment, judged by ctest's rules."""
    root = project("calc_cmake")
    timed = adapter(root, **CTEST).tests.benchmark(BenchmarkRequest(
        _ctest_suite(root), runs=2, timeout_s=300))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert len(timed.times_ms) == 2 and "ctest" in timed.includes
    # With a limit of 20, high (50, expecting 10) fails, and wrong_on_purpose still passes.
    failing = adapter(root, **CTEST, c_build="-DCALC_LIMIT=20").tests.benchmark(
        BenchmarkRequest(_ctest_suite(root), runs=2, timeout_s=300))
    assert failing.outcome is la.Outcome.TEST_FAILURES, failing.reason
    assert "high" in failing.reason and "wrong_on_purpose" not in failing.reason


def test_ctests_tests_give_per_test_evidence(project, adapter):
    """Each test's own lines. The test a_script runs a script, which no link
    of the build wrote: it is skipped, writes no counters file and has zero
    counts, and the evidence completes."""
    from langadapter.runs import PerTestEvidenceRequest
    from langadapter.suites import LineItem

    root = project("calc_cmake")
    evidence = adapter(root, **CTEST).per_test_evidence(PerTestEvidenceRequest(
        _ctest_suite(root), target="src/calc.c", timeout_s=300))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    lines = {e.test.qualname: {i.line for i in e.items if isinstance(i, LineItem)}
             for e in evidence.tests}
    assert 6 in lines["low"] and 9 not in lines["low"]
    assert 9 in lines["high"] and 6 not in lines["high"]
    assert lines["a_script"] == set()


def test_a_test_a_function_of_another_file_adds_runs_in_its_callers_dir(project, adapter):
    """ctest runs a test without a WORKING_DIRECTORY in the build directory of
    the CMakeLists.txt being processed, also when a function defined in
    another file adds the test."""
    root = project("calc_cmake")
    (root / "cmake").mkdir()
    (root / "cmake" / "checks.cmake").write_text(
        "function(add_check name)\n  add_test(NAME ${name} COMMAND test-dir)\n"
        "endfunction()\n", encoding="utf-8")
    (root / "test" / "dir").mkdir()
    (root / "test" / "dir" / "CMakeLists.txt").write_text(
        'file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/marker.txt "here\\n")\n'
        "add_check(in_its_dir)\n", encoding="utf-8")
    (root / "test" / "test-dir.c").write_text(
        "#include <stdio.h>\n\nint main(void) {\n"
        '    return fopen("marker.txt", "r") != NULL ? 0 : 1;\n}\n', encoding="utf-8")
    with open(root / "CMakeLists.txt", "a", encoding="utf-8") as handle:
        handle.write("include(cmake/checks.cmake)\n"
                     "add_executable(test-dir test/test-dir.c)\n"
                     "target_link_libraries(test-dir calc)\n"
                     "add_subdirectory(test/dir)\n")
    run = adapter(root, **CTEST).run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    [test] = [t for t in run.tests if t.test.qualname == "in_its_dir"]
    assert test.outcome == "passed", test.message


def test_ctests_skip_patterns_and_environment_modification(project, adapter):
    """A test whose output matches its SKIP_REGULAR_EXPRESSION is skipped as
    ctest skips it, and a test with ENVIRONMENT_MODIFICATION, which the
    adapter does not apply, is reported skipped with the reason."""
    root = project("calc_cmake")
    (root / "test" / "test-skip.c").write_text(
        "#include <stdio.h>\n\nint main(void) {\n"
        '    puts("skipping: no input data");\n    return 1;\n}\n', encoding="utf-8")
    with open(root / "CMakeLists.txt", "a", encoding="utf-8") as handle:
        handle.write("add_executable(test-skip test/test-skip.c)\n"
                     "target_link_libraries(test-skip calc)\n"
                     "add_test(NAME skipped_by_output COMMAND test-skip)\n"
                     "set_tests_properties(skipped_by_output PROPERTIES\n"
                     "  SKIP_REGULAR_EXPRESSION \"^skipping\")\n"
                     "add_test(NAME modified COMMAND test-args 50)\n"
                     "set_tests_properties(modified PROPERTIES\n"
                     "  ENVIRONMENT_MODIFICATION \"CALC_EXPECT=set:10\")\n")
    run = adapter(root, **CTEST).run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    tests = {t.test.qualname: t for t in run.tests}
    assert tests["skipped_by_output"].outcome == "skipped", tests["skipped_by_output"].message
    assert "SKIP_REGULAR_EXPRESSION" in tests["skipped_by_output"].message
    assert tests["modified"].outcome == "skipped"
    assert "ENVIRONMENT_MODIFICATION" in tests["modified"].message


CHATTY = """\
#include <stdio.h>
#include <stdlib.h>

#include "calc.h"

/* Writes 32 MiB to stdout and 8 MiB to stderr, then its last line, and ends
   with the status its first argument names. */
int main(int argc, char **argv) {
    static char line[1024];
    for (int i = 0; i < 1023; i++) {
        line[i] = 'x';
    }
    line[1023] = '\\n';
    for (int i = 0; i < 32 * 1024; i++) {
        fwrite(line, 1, sizeof line, stdout);
    }
    for (int i = 0; i < 8 * 1024; i++) {
        fwrite(line, 1, sizeof line, stderr);
    }
    fputs(argc > 2 ? argv[2] : "the last line", stderr);
    fputs("\\n", stderr);
    return argc > 1 ? atoi(argv[1]) + (clamp_to_limit(0) != 0) : 2;
}
"""


def _with_chatty(root, tests):
    (root / "test" / "test-chatty.c").write_text(CHATTY, encoding="utf-8")
    with open(root / "CMakeLists.txt", "a", encoding="utf-8") as handle:
        handle.write("add_executable(test-chatty test/test-chatty.c)\n"
                     "target_link_libraries(test-chatty calc)\n" + tests)


def test_a_test_that_writes_far_more_than_is_kept_keeps_its_verdict(project, adapter):
    """Each stream of a test process is kept whole up to twice OUTPUT_KEPT
    bytes, else its start and its end around a line that counts what was
    left out: a test that writes far more keeps its verdict and the end of
    its output, which its message shows."""
    root = project("calc_cmake")
    _with_chatty(root, "add_test(NAME chatty_passes COMMAND test-chatty 0)\n"
                       "add_test(NAME chatty_fails COMMAND test-chatty 1)\n")
    run = adapter(root, **CTEST).run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    tests = {t.test.qualname: t for t in run.tests}
    assert tests["chatty_passes"].outcome == "passed", tests["chatty_passes"].message
    assert tests["chatty_fails"].outcome == "failed", tests["chatty_fails"].message
    assert tests["chatty_fails"].message.endswith("the last line")
    # 80 MiB written, each of its four streams kept to about 2 MiB.
    assert len(run.output) < 9 * 1024 * 1024
    assert run.output.count("bytes of the test output omitted ...]") == 4


def test_a_test_its_regular_expressions_judge_keeps_its_whole_output(project, adapter):
    """ctest's PASS_REGULAR_EXPRESSION decides by the whole output, so a test
    that has one keeps it whole, and a match after the bound still passes it."""
    root = project("calc_cmake")
    _with_chatty(root, 'add_test(NAME judged COMMAND test-chatty 1 "all checks done")\n'
                       "set_tests_properties(judged PROPERTIES\n"
                       '  PASS_REGULAR_EXPRESSION "all checks done")\n')
    run = adapter(root, **CTEST).run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    [judged] = [t for t in run.tests if t.test.qualname == "judged"]
    assert judged.outcome == "passed", judged.message
    assert "omitted" not in run.output and len(run.output) > 40 * 1024 * 1024


def test_a_run_dirs_tests_are_read_from_its_file_each_time(tmp_path):
    """A run directory's tests are those its file holds now: a later directory
    at the path of a removed one does not get the removed one's tests."""
    import json

    from langadapter.c import _ctest

    programs_dir = tmp_path / "programs" / "program-0"
    for name in ("first", "second"):
        programs_dir.mkdir(parents=True)
        (programs_dir / _ctest.SPECS).write_text(json.dumps({name: {"reason": None}}),
                                           encoding="utf-8")
        assert _ctest.list_tests(programs_dir, None, cwd="", env={}, timeout_s=1) == (name,)
        shutil.rmtree(programs_dir)


def test_ctest_needs_a_cmake_project(project, adapter):
    root = project("calc_programs")
    run = adapter(root, c_tests="ctest", c_build_kind="flags", c_flags="-Isrc",
                  c_link="src/*.c", c_target="src/calc.c").run_suite(
        SuiteRunRequest(la.Suite(str(root), ("test/test-calc.c",)), timeout_s=120))
    assert run.outcome is la.Outcome.UNSUPPORTED and "CMake project" in run.reason_text(None)


def test_named_ctest_tests_get_extra_arguments_and_the_seed(project, adapter):
    """c_ctest_arguments gives named tests arguments after ctest's own, as a
    test that seeds itself from its first argument needs (libcbor's
    fuzz_test). {seed} is 1, since a C run carries no session seed."""
    from langadapter.c import _ctest

    root = project("calc_cmake")
    with open(root / "CMakeLists.txt", "a", encoding="utf-8") as handle:
        handle.write("add_test(NAME seeded COMMAND test-args)\n"
                     "add_test(NAME given COMMAND test-args 50)\n")
    request = SuiteRunRequest(_ctest_suite(root), timeout_s=300)

    def verdicts(**settings):
        run = adapter(root, **CTEST, **settings).run_suite(request)
        assert run.tests, run.reason_text(None)
        return {t.test.qualname: t.outcome for t in run.tests}

    before = verdicts()
    assert before["seeded"] == before["given"] == "failed"
    # test-args passes when its first argument clamps to its second: only seed 1 does.
    after = verdicts(c_ctest_arguments="seeded {seed} 1; given 10")
    assert after["seeded"] == after["given"] == "passed"
    assert _ctest.SEED == 1


def test_extra_arguments_for_a_test_ctest_does_not_list_are_refused(project, adapter):
    root = project("calc_cmake")
    run = adapter(root, **CTEST, c_ctest_arguments="no_such_test 1").run_suite(
        SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    assert run.outcome is la.Outcome.COLLECTION_FAILED
    assert "no_such_test" in run.reason_text(None)


@needs_mull
def test_a_mutation_run_of_ctests_tests(project, adapter):
    from langadapter.mutation import MutationRequest

    from langadapter.c import _mull

    root = project("calc_cmake")
    with adapter(root, **CTEST).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target="src/calc.c", suite=_ctest_suite(root),
                                          attribution="all"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    by = {(m.key.operator, m.key.line): m for m in run.mutants}
    # x > CALC_LIMIT turned to x <= CALC_LIMIT: every test that clamps above fails.
    assert set(by[("cxx_gt_to_le", 8)].killers) >= {"CMakeLists.txt::high",
                                                    "CMakeLists.txt::from_environment"}
    assert all(m.row["native_status"] == "not_executed" for m in run.mutants
               if m.key.line == 15)


@pytest.mark.parametrize("raised, outcome", [
    (FileNotFoundError(2, "No such file or directory", "ctest"), la.Outcome.MISSING_SETUP),
    ("timeout", la.Outcome.TIMED_OUT)])
def test_a_ctest_listing_that_cannot_run_names_its_outcome(project, adapter, monkeypatch,
                                                           raised, outcome):
    """ctest that does not start is missing setup and a listing past its time
    is a timeout, not a failed collection."""
    import subprocess

    root = project("calc_cmake")
    c = adapter(root, **CTEST)
    run = c._run

    def without_ctest(argv, **options):
        if argv[0] == "ctest":
            raise (subprocess.TimeoutExpired(argv, 1) if raised == "timeout" else raised)
        return run(argv, **options)

    monkeypatch.setattr(c, "_run", without_ctest)
    result = c.run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    assert result.outcome is outcome, result.reason_text(None)
    assert "ctest could not list" in result.reason_text(None)


def test_a_ctest_relink_past_the_runs_time_is_a_timeout(project, adapter, monkeypatch):
    """A relink of ctest's test programs that reaches the run's timeout is a
    timeout, as the link of a test file is, not a failed collection."""
    from langadapter.c import _build

    root = project("calc_cmake")
    relinks = []
    program_command = _build.TargetBuild.program_command

    def recorded(self, *args, **options):
        relinks.append(program_command(self, *args, **options))
        return relinks[-1]

    monkeypatch.setattr(_build.TargetBuild, "program_command", recorded)
    c = adapter(root, **CTEST)
    compile_ = c._compile

    def relink_times_out(argv, cwd, deadline, extra=None):
        if any(argv is relink for relink in relinks):
            return None
        return compile_(argv, cwd, deadline, extra)

    monkeypatch.setattr(c, "_compile", relink_times_out)
    result = c.run_suite(SuiteRunRequest(_ctest_suite(root), timeout_s=300))
    assert result.outcome is la.Outcome.TIMED_OUT, result.reason_text(None)
    assert "the run reached its timeout while relinking the tests" in result.reason_text(None)
