"""Python tests of a C extension, through the real GCC, gcov, pytest and Mull."""
from __future__ import annotations

import dataclasses
import gc
import sys
import tempfile
from pathlib import Path

import pytest

import langadapter as la
from langadapter.errors import InvalidRequest
from langadapter.mutation import (
    KILLED_STATUS, NON_VIABLE, NON_VIABLE_STATUS, UNTESTED_STATUS, Catalogue, Classification,
    MutationClass, MutationRequest,
)
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest
from langadapter.sources import PoolMember
from langadapter.suites import LineItem

from langadapter.c import _mull
from langadapter.c.adapter import CAdapter
from conftest import (PROJECTS, needs_gcc, needs_mull, needs_python_headers,
                      negative_count_output)

pytestmark = [needs_gcc, needs_python_headers]

TARGET = "calcext/_calc.c"
SOURCE = (PROJECTS / "calc_ext" / TARGET).read_text().splitlines()


def line(text: str, after: str = "") -> int:
    """The number of the fixture's first line holding ``text`` after the line
    holding ``after``."""
    start = next(i for i, found in enumerate(SOURCE) if after in found) if after else 0
    return next(i + 1 for i, found in enumerate(SOURCE) if i >= start and text in found)


CLAMP_IF_LOW = line("if (x < lo)")
CLAMP_LOW = line("return lo;")
CLAMP_IF_HIGH = line("if (x > hi)")
CLAMP_X = line("return x;")
PARSE = line("if (!PyArg_ParseTuple")
CLAMP_RETURN = line("return PyLong_FromLong(clamp(")
INIT = line("PyObject *made = PyModule_Create(&module);")
INIT_CHECK = line("if (made == NULL)")
# calc_sign from its head to its closing brace, the line after its return.
SIGN = (line("static PyObject *calc_sign"),
        line("x < 0 ? -1 : 0", after="static PyObject *calc_sign") + 1)
HEAD_TEST = line("if (table == NULL)")

TESTS = """\
from calcext import _calc


def test_clamp_below():
    assert _calc.clamp(-5, 0, 10) == 0


def test_clamp_inside():
    assert _calc.clamp(5, 0, 10) == 5
"""


@pytest.fixture
def python_adapter():
    """A C adapter for the project at ``root`` whose tests are Python, run by
    the interpreter of these tests (it has pytest and setuptools)."""
    def make(root: Path, **settings) -> CAdapter:
        context = la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=sys.executable),
                                    environment=CAdapter.context_environment(settings))
        return CAdapter(context)
    return make


def _built(root: Path):
    return sorted(p.relative_to(root) for p in root.rglob("*")
                  if "__pycache__" not in p.parts)


def test_the_runtime_of_python_tests_is_the_python_their_settings_select(project):
    root = project("calc_ext")
    names = [setting.name for setting in CAdapter.project_settings]
    assert {"python_executable", "python_version", "frozen_module_strategy",
            "pytest_config"} <= set(names)
    choice = CAdapter.discover_runtime(str(root), {"python_executable": sys.executable,
                                                   "frozen_module_strategy": "unfreeze",
                                                   "pytest_config": "none"})
    assert choice.interpreter.path == str(Path(sys.executable).absolute())
    assert choice.interpreter_flags == ("-X", "frozen_modules=off")
    assert choice.test_framework.source == "isolated"


def test_a_setup_py_extension_gives_python_tests(project, python_adapter):
    root = project("calc_ext")
    adapter = python_adapter(root)
    assert adapter._extension_mode
    assert (adapter.test_file_name, adapter.code_fence, adapter.test_framework) == (
        "test_{stem}.py", "python", "pytest")
    assert CAdapter.test_file_name == "test_{stem}.c"
    words = adapter.test_vocabulary()
    assert words["import_rule"] == ("imports the compiled extension module built from the "
                                    "target C file")
    assert "pytest" in words["test_dependencies"]
    # gcov counts the target for Python tests too, but a Python test seldom
    # corrupts memory: no cause and no fix of counts that cannot be read.
    for name in ("coverage_counts_inconsistent_cause", "coverage_counts_inconsistent_fix"):
        assert words[name] == "", name
    facts = adapter.language_facts()
    assert (facts.language, facts.code_fence, facts.test_language, facts.target_code_fence) == (
        "C", "python", "python", "c")
    assert CAdapter.import_statement(TARGET, str(root)) == "from calcext import _calc"
    # The source operations of the tests are Python's, merging's separator too.
    assert adapter.source_merge_separator == "#"
    assert CAdapter.source_merge_separator == "//"


def test_a_source_only_context_reports_the_tests_language(project):
    facts = CAdapter(la.ProjectContext(str(project("calc_ext")))).language_facts()
    assert (facts.language, facts.test_language, facts.target_code_fence,
            facts.runtime_version) == ("C", "python", "c", None)


def test_python_tests_are_found_and_removed_by_python_s_operations(project, python_adapter):
    adapter = python_adapter(project("calc_ext"))
    assert adapter.test_units is None
    pruned = adapter.sources.prune_tests(TESTS, ["test_clamp_below"])
    assert "test_clamp_below" not in pruned.source and "test_clamp_inside" in pruned.source
    assert list(adapter.sources.duplicate_test_names(TESTS + TESTS.split("\n", 2)[2]).value) == [
        "test_clamp_below", "test_clamp_inside"]
    # The tests' structure is Python's, the target's metrics C's.
    assert adapter.skeleton(TESTS) == ("test_clamp_below", "test_clamp_inside")
    assert adapter.same_skeleton(TESTS, TESTS) == (True, "")
    assert adapter.optimization_metrics("\n".join(SOURCE))["function_count"] > 0
    assert adapter.sources.supports("metrics")
    identity = adapter.structural_parser_identity()
    assert "tree-sitter-python" in identity and "tree-sitter-c" in identity


SRC_TARGET = "src/calcext/_calc.c"


@pytest.mark.parametrize("settings", [{}, {"python_import_root": "src"}])
def test_a_src_layout_imports_its_package_from_src(project, python_adapter, suite, settings):
    """The tests import the package from src/, which python_import_root
    names or, without it, setup.py's extension implies: the
    extension loads and its lines are measured."""
    root = project("calc_ext_src")
    run = python_adapter(root, **settings).run_suite(SuiteRunRequest(
        suite(TESTS, "test__calc.py"), timeout_s=180, coverage_target=SRC_TARGET))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert sorted(t.test.qualname for t in run.tests) == ["test_clamp_below", "test_clamp_inside"]
    assert run.coverage.target.path == SRC_TARGET and run.coverage.covered


@pytest.mark.parametrize("settings", [{"c_target": SRC_TARGET}, {}])
def test_a_src_layout_lists_tests_whose_conftest_imports_its_extension(
        project, python_adapter, settings):
    """The tests are listed as they run, from the import root with the
    target's extension staged, so a conftest.py that imports the project's
    package collects, as markupsafe's does. Without c_target the
    target is the only extension setup.py names."""
    root = project("calc_ext_src")
    tests = root / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("from calcext import _calc\n", encoding="utf-8")
    (tests / "test_calc.py").write_text(TESTS, encoding="utf-8")
    assert python_adapter(root, **settings).test_files([str(tests)]) == [
        str(tests / "test_calc.py")]


def test_a_compiler_that_does_not_run_fails_the_listing_with_its_reason(
        project, python_adapter, tmp_path):
    """Listing the Python tests stages the target's extension, which needs
    GCC. A compiler that does not run fails the listing with the reason a
    run gives for its missing setup."""
    root = project("calc_ext")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(TESTS, encoding="utf-8")
    broken = tmp_path / "gcc"
    broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    adapter = python_adapter(root, c_target=TARGET, c_compiler=str(broken))
    with pytest.raises(RuntimeError, match="does not run"):
        adapter.test_files([str(tests)])


def test_a_project_whose_warnings_are_errors_lists_and_runs_its_tests(project, python_adapter):
    """A project whose pytest configuration turns every warning into an
    error, as markupsafe's filterwarnings = ["error"] does, lists and runs
    its tests: the harness's plugin, imported before pytest loads it, asks
    pytest for no assertion rewrite and so raises no warning."""
    root = project("calc_ext_src")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nfilterwarnings = [\"error\"]\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("from calcext import _calc\n", encoding="utf-8")
    (tests / "test_calc.py").write_text(TESTS, encoding="utf-8")
    adapter = python_adapter(root, c_target=SRC_TARGET)
    assert adapter.test_files([str(tests)]) == [str(tests / "test_calc.py")]
    run = adapter.run_suite(SuiteRunRequest(
        la.Suite(str(tests), ("test_calc.py",), ("conftest.py",)),
        timeout_s=180, coverage_target=SRC_TARGET))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert run.counts.passed == 2


def test_tests_of_no_known_target_ask_for_c_target(project, python_adapter):
    """With two extensions, no c_target and nothing built, no extension is
    staged and the tests collect from the project directory. A collection that
    fails names c_target, the setting that fixes it. With it set
    the tests are listed."""
    root = project("calc_ext_src")
    (root / "setup.py").write_text(
        "from setuptools import Extension, setup\n\n"
        "setup(name='calcext', version='0', package_dir={'': 'src'}, packages=['calcext'],\n"
        "      ext_modules=[Extension('calcext._calc', sources=['src/calcext/_calc.c']),\n"
        "                   Extension('calcext._more', sources=['src/calcext/_more.c'])])\n",
        encoding="utf-8")
    calc = (root / "src" / "calcext" / "_calc.c").read_text(encoding="utf-8")
    (root / "src" / "calcext" / "_more.c").write_text(
        calc.replace('"_calc"', '"_more"').replace("PyInit__calc", "PyInit__more"),
        encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("import calcext\n", encoding="utf-8")
    (tests / "test_calc.py").write_text(TESTS, encoding="utf-8")
    with pytest.raises(RuntimeError, match="set c_target to the C file the tests are for"):
        python_adapter(root).test_files([str(tests)])
    assert python_adapter(root, c_target=SRC_TARGET).test_files([str(tests)]) == [
        str(tests / "test_calc.py")]


def test_a_namespace_packages_own_tests_run_against_its_extension(project, python_adapter):
    """A namespace package (PEP 420) in a src layout whose tests lie inside
    it, as zope.hookable's do, needs no setting."""
    root = project("calc_ext_ns")
    tests = root / "src" / "nsx" / "calc" / "tests"
    run = python_adapter(root).run_suite(SuiteRunRequest(
        la.Suite(str(tests), ("test_calc.py",)), timeout_s=180,
        coverage_target="src/nsx/calc/_calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert run.counts.passed == 2 and run.coverage.covered


def test_each_adapters_test_environment_reaches_only_its_runs(project, python_adapter, suite):
    """c_test_environment reaches the C adapter's runs, Python tests of its
    extension among them, and python_test_environment reaches only the Python
    adapter's own runs."""
    values = {"python_test_environment": "PURE_PYTHON=1", "c_test_environment": "C_ONLY=yes"}
    variables = CAdapter.context_environment(values)
    assert variables["C_ONLY"] == "yes" and "PURE_PYTHON" not in variables
    assert PythonAdapter.context_environment(values) == {"PURE_PYTHON": "1"}
    checked = TESTS + ("\n\ndef test_the_environment():\n    import os\n"
                       "    assert os.environ.get('C_ONLY') == 'yes'\n"
                       "    assert 'PURE_PYTHON' not in os.environ\n")
    run = python_adapter(project("calc_ext"), **values).run_suite(SuiteRunRequest(
        suite(checked, "test__calc.py"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert run.counts.passed == 3


def test_python_tests_measure_the_c_file(project, python_adapter, suite):
    root = project("calc_ext")
    before = _built(root)
    run = python_adapter(root).run_suite(SuiteRunRequest(
        suite(TESTS, "test__calc.py"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert sorted(t.test.qualname for t in run.tests) == ["test_clamp_below", "test_clamp_inside"]
    covered = set(run.coverage.covered)
    assert {CLAMP_IF_LOW, CLAMP_LOW, CLAMP_IF_HIGH, CLAMP_X, PARSE, CLAMP_RETURN, INIT} <= covered
    assert not covered & set(range(SIGN[0] + 1, SIGN[1]))
    assert run.coverage.target.path == TARGET
    # No build output lands in the project (its Python writes bytecode, as
    # Python's own runs do).
    assert _built(root) == before


def test_each_python_test_has_its_own_c_lines(project, python_adapter, suite):
    root = project("calc_ext")
    evidence = python_adapter(root).per_test_evidence(PerTestEvidenceRequest(
        suite(TESTS, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    lines = {e.test.qualname: {i.line for i in e.items if isinstance(i, LineItem)}
             for e in evidence.tests}
    assert CLAMP_LOW in lines["test_clamp_below"] and CLAMP_X not in lines["test_clamp_below"]
    assert CLAMP_X in lines["test_clamp_inside"] and CLAMP_LOW not in lines["test_clamp_inside"]
    # The module's init ran at import, outside every test.
    assert not any(INIT in found for found in lines.values())
    assert LineItem(TARGET, INIT) in evidence.outside_items


def test_a_python_test_that_executes_nothing_of_the_extension_gets_zero_counts(
        project, python_adapter, suite):
    """The plugin's write at a test's end gives a test that executes nothing
    of the target a counters file with zero counts: its evidence completes
    with no lines, where a missing file would end it naming the test."""
    idle = TESTS + "\n\ndef test_nothing_of_the_extension():\n    assert 1 + 1 == 2\n"
    evidence = python_adapter(project("calc_ext")).per_test_evidence(PerTestEvidenceRequest(
        suite(idle, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    assert evidence.stopping_tests == ()
    lines = {e.test.qualname: {i.line for i in e.items if isinstance(i, LineItem)}
             for e in evidence.tests}
    assert lines["test_nothing_of_the_extension"] == set()


def _counts_refused(root: Path, gcovr_stand_in) -> CAdapter:
    """A C adapter for Python tests of ``root`` whose gcovr refuses the
    target's counts as negative."""
    environment = {**CAdapter.context_environment({}),
                   **gcovr_stand_in(64, negative_count_output(TARGET))}
    return CAdapter(la.ProjectContext(root=str(root), interpreter=la.Interpreter(
        path=sys.executable), environment=environment))


NEGATIVE_COUNT = f"{TARGET}:2 Got negative hit value in: branch  0 taken -2 (fallthrough)"


def test_negative_counts_end_python_tests_as_an_infrastructure_failure(project, suite,
                                                                       gcovr_stand_in):
    from langadapter.runs import RunDiagnostic

    run = _counts_refused(project("calc_ext"), gcovr_stand_in).run_suite(SuiteRunRequest(
        suite(TESTS, "test__calc.py"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason_text(None)
    assert run.reason == NEGATIVE_COUNT
    assert run.diagnostics[-1] == RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT)
    assert sorted((t.test.qualname, t.outcome) for t in run.tests) == [
        ("test_clamp_below", "passed"), ("test_clamp_inside", "passed")]
    assert run.coverage is None


def test_negative_counts_leave_failing_python_tests_their_outcome(project, suite,
                                                                gcovr_stand_in):
    from langadapter.runs import RunDiagnostic

    failing = TESTS + "\n\ndef test_clamp_wrong():\n    assert _calc.clamp(5, 0, 10) == 4\n"
    run = _counts_refused(project("calc_ext"), gcovr_stand_in).run_suite(SuiteRunRequest(
        suite(failing, "test__calc.py"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert run.diagnostics[-1] == RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT)
    assert sorted((t.test.qualname, t.outcome) for t in run.tests) == [
        ("test_clamp_below", "passed"), ("test_clamp_inside", "passed"),
        ("test_clamp_wrong", "failed")]
    assert run.coverage is None


def test_negative_counts_leave_failing_python_tests_evidence_its_outcome(project, suite,
                                                                       gcovr_stand_in):
    failing = TESTS + "\n\ndef test_clamp_wrong():\n    assert _calc.clamp(5, 0, 10) == 4\n"
    evidence = _counts_refused(project("calc_ext"), gcovr_stand_in).per_test_evidence(
        PerTestEvidenceRequest(suite(failing, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.TEST_FAILURES, evidence.reason


def test_negative_counts_end_python_tests_evidence_as_an_infrastructure_failure(
        project, suite, gcovr_stand_in):
    evidence = _counts_refused(project("calc_ext"), gcovr_stand_in).per_test_evidence(
        PerTestEvidenceRequest(suite(TESTS, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT


FAILING = TESTS + "\n\ndef test_clamp_wrong():\n    assert _calc.clamp(5, 0, 10) == 4\n"


def test_extension_evidence_names_the_tests_that_do_not_pass(project, python_adapter, suite):
    """The names are the test ids of the suite run's own result."""
    adapter = python_adapter(project("calc_ext"))
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite(FAILING, "test__calc.py"), target=TARGET, timeout_s=180))
    run = adapter.run_suite(SuiteRunRequest(suite(FAILING, "test__calc.py"), timeout_s=180))
    assert evidence.outcome is la.Outcome.TEST_FAILURES, evidence.reason
    assert [t.qualname for t in evidence.stopping_tests] == ["test_clamp_wrong"]
    assert evidence.stopping_tests == run.failing_tests()


def _refusing_clamp_low(root: Path, gcovr_refusing) -> CAdapter:
    environment = {**CAdapter.context_environment({}),
                   **gcovr_refusing(CLAMP_LOW, negative_count_output(TARGET))}
    return CAdapter(la.ProjectContext(root=str(root), interpreter=la.Interpreter(
        path=sys.executable), environment=environment))


def test_extension_evidence_names_the_test_whose_own_counts_gcovr_refuses(
        project, suite, gcovr_refusing):
    evidence = _refusing_clamp_low(project("calc_ext"), gcovr_refusing).per_test_evidence(
        PerTestEvidenceRequest(suite(TESTS, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT
    assert [t.qualname for t in evidence.stopping_tests] == ["test_clamp_below"]


def test_an_extension_test_whose_folder_holds_no_counters_is_named(project, python_adapter,
                                                                   suite, monkeypatch):
    """The plugin wrote the second test's folder, and its counters file is
    missing: the test did not reach the plugin's write."""
    from langadapter.c import _gcov

    counters_file = _gcov.counters_file
    monkeypatch.setattr(_gcov, "counters_file", lambda prefix, notes: (
        None if prefix.name == "t1" else counters_file(prefix, notes)))
    evidence = python_adapter(project("calc_ext")).per_test_evidence(PerTestEvidenceRequest(
        suite(TESTS, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert [t.qualname for t in evidence.stopping_tests] == ["test_clamp_inside"]
    assert evidence.reason == (f"{evidence.stopping_tests[0].text}: "
                               "no coverage counters were written for it")


def test_an_extension_test_without_a_counters_folder_names_no_test(project, python_adapter,
                                                                   suite, monkeypatch):
    """A node id that maps to none of the plugin's folders is the adapter's
    gap, not the test's fault."""
    test_of_nodeid = PythonAdapter.test_of_nodeid
    monkeypatch.setattr(PythonAdapter, "test_of_nodeid", staticmethod(lambda nodeid: (
        dataclasses.replace(test_of_nodeid(nodeid), qualname="elsewhere")
        if nodeid.endswith("test_clamp_inside") else test_of_nodeid(nodeid))))
    evidence = python_adapter(project("calc_ext")).per_test_evidence(PerTestEvidenceRequest(
        suite(TESTS, "test__calc.py"), target=TARGET, timeout_s=180))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.stopping_tests == ()
    assert evidence.reason.endswith("test_clamp_inside: no counters folder of the plugin "
                                    "belongs to it"), evidence.reason


@needs_mull
def test_the_check_of_python_tests_names_the_tests_that_do_not_pass(project, python_adapter,
                                                                    suite):
    with python_adapter(project("calc_ext")).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target=TARGET, suite=suite(FAILING, "test__calc.py")))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert [t.qualname for t in run.stopping_tests] == ["test_clamp_wrong"]


@needs_mull
def test_the_check_of_python_tests_names_a_test_that_fails_only_on_mulls_build(
        project, python_adapter, suite):
    tests = TESTS.replace("from calcext import _calc\n",
                          "import os\n\nfrom calcext import _calc\n") + (
        "\n\ndef test_not_on_mulls_build():\n"
        "    assert os.sep + \"mull\" + os.sep not in _calc.__file__\n")
    with python_adapter(project("calc_ext")).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target=TARGET, suite=suite(tests, "test__calc.py")))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert "on Mull's build" in run.reason
    assert [t.qualname for t in run.stopping_tests] == ["test_not_on_mulls_build"]


def test_python_tests_keep_a_captured_pythonpath(project, suite, tmp_path):
    # Testuary captures the run's whole environment as the context's own.
    import os

    root = project("calc_ext")
    extra = tmp_path / "user_lib"
    extra.mkdir()
    captured = la.Environment({**os.environ, **CAdapter.context_environment({}),
                               "PYTHONPATH": str(extra)}, inherit=False)
    context = la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=sys.executable),
                                environment=captured)
    tests = f"""\
import sys


def test_the_users_path_stays():
    assert {str(extra)!r} in sys.path
"""
    run = CAdapter(context).run_suite(SuiteRunRequest(suite(tests, "test__calc.py"),
                                                      timeout_s=180))
    assert run.outcome is la.Outcome.COMPLETED, run.reason


def test_an_address_space_limit_bounds_python_tests_of_an_extension(project, python_adapter,
                                                                   suite):
    root = project("calc_ext")
    tests = """\
def test_three_gib():
    assert len(bytearray(3 << 30)) == 3 << 30
"""
    free = python_adapter(root).run_suite(SuiteRunRequest(suite(tests, "test__calc.py"),
                                                          timeout_s=180))
    assert free.outcome is la.Outcome.COMPLETED, free.reason
    bounded = python_adapter(root, c_address_space_limit="1G").run_suite(
        SuiteRunRequest(suite(tests, "test__calc.py"), timeout_s=180))
    assert bounded.outcome is la.Outcome.TEST_FAILURES, bounded.reason


def test_an_address_space_limit_set_as_a_size_bounds_python_tests(project, suite):
    # A program may set the variable itself, in any form the setting takes.
    root = project("calc_ext")
    tests = """def test_three_gib():
    assert len(bytearray(3 << 30)) == 3 << 30
"""
    context = la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=sys.executable),
                                environment={"LANGADAPTER_C_ADDRESS_SPACE_LIMIT": "1G"})
    run = CAdapter(context).run_suite(SuiteRunRequest(suite(tests, "test__calc.py"),
                                                      timeout_s=180))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason


def test_a_run_configuration_it_cannot_read_leaves_the_interpreter_as_usual(tmp_path):
    import os
    import shutil
    import subprocess

    import langadapter.c

    here = Path(langadapter.c.__file__).parent
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    shutil.copyfile(here / "_pytest_plugin.py", plugin / "langadapter_c_plugin.py")
    shutil.copyfile(here / "_sitecustomize.py", plugin / "sitecustomize.py")
    config = tmp_path / "plugin.cfg"
    config.write_bytes(b"module\t\xff\xfe\n")
    done = subprocess.run([sys.executable, "-c", "print('started')"], capture_output=True,
                          text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(plugin),
                               "LANGADAPTER_C_PLUGIN_CONFIG": str(config)})
    assert done.stdout.strip() == "started"
    assert "Error in sitecustomize" not in done.stderr


def test_a_run_that_ends_before_any_session_keeps_its_own_outcome(project, python_adapter,
                                                                  suite):
    # pytest stops while it loads the suite's conftest, before a session.
    root = project("calc_ext")
    tests = suite("def test_clamp():\n    pass\n", "test__calc.py")
    (Path(tests.root) / "conftest.py").write_text("raise RuntimeError('conftest broke')\n",
                                                  encoding="utf-8")
    tests = la.Suite(tests.root, tests.test_files, ("conftest.py",))
    adapter = python_adapter(root)
    run = adapter.run_suite(SuiteRunRequest(tests, timeout_s=180))
    assert "was not loaded" not in run.reason, run.reason
    assert "conftest broke" in run.reason_text(), run.reason_text()
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        tests, target=TARGET, timeout_s=180))
    assert "was not loaded" not in evidence.reason, evidence.reason


def test_a_broken_extension_is_not_measured_through_the_fallback(project, python_adapter,
                                                                 suite):
    root = project("calc_ext")
    source = root / TARGET
    # The init fails with ImportError, so calcext falls back to its Python
    # functions and the tests pass without the extension.
    source.write_text(source.read_text().replace(
        "return made;", 'PyErr_SetString(PyExc_ImportError, "broken"); return NULL;'))
    run = python_adapter(root).run_suite(SuiteRunRequest(suite("""\
import calcext


def test_clamp():
    assert calcext.clamp(-5, 0, 10) == 0
""", "test__calc.py"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE
    assert "did not load" in run.reason
    assert run.counts.passed == 1


def test_python_tests_run_as_delivered_against_the_extension(project, python_adapter, suite):
    root = project("calc_ext")
    tests = suite(TESTS, "test__calc.py")
    adapter = python_adapter(root)
    assert adapter.tests.supports("run_as_delivered")
    request = SuiteRunRequest(tests, timeout_s=180, session_seed=1)
    run = adapter.tests.run_as_delivered(request)
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert sorted(t.test.text for t in run.tests) == [
        "test__calc.py::test_clamp_below", "test__calc.py::test_clamp_inside"]
    assert ("gcc", adapter._toolchain().version) in run.identity.toolchain.tools
    # A replacement text runs in the file's place.
    run = adapter.tests.run_as_delivered(
        request, sources={"test__calc.py": TESTS.replace("== 5", "== 6")})
    assert run.outcome is la.Outcome.TEST_FAILURES
    assert [t.test.qualname for t in run.tests if t.outcome == "failed"] == [
        "test_clamp_inside"]


SEEDED = """\
import os

from calcext import _calc


def test_the_session_seed_reaches_the_run():
    assert os.environ["PYTHONHASHSEED"] == "7"
    assert _calc.clamp(5, 0, 10) == 5
"""

HANGING = """\
import time

from calcext import _calc


def test_clamp_inside():
    assert _calc.clamp(5, 0, 10) == 5


def test_hangs():
    time.sleep(600)
"""


def test_python_tests_of_an_extension_state_pythons_seed_and_journal(project, python_adapter,
                                                                     suite):
    """The adapter states the facts of its Python tests' runs, which take the
    session seed and the timeout journal. The class keeps C's facts: C's runs
    as delivered take no seed."""
    adapter = python_adapter(project("calc_ext"))
    assert (adapter.seeds_delivered_runs, adapter.provides_timeout_journal) == (True, True)
    assert (CAdapter.seeds_delivered_runs, CAdapter.provides_timeout_journal) == (False, True)
    seeded = adapter.tests.run_as_delivered(SuiteRunRequest(
        suite(SEEDED, "test__calc.py"), timeout_s=180, session_seed=7))
    assert seeded.outcome is la.Outcome.COMPLETED, seeded.reason_text(None)
    hung = adapter.tests.run_suite(SuiteRunRequest(
        suite(HANGING, "test__calc.py"), timeout_s=60, timeout_journal=True))
    assert hung.outcome is la.Outcome.TIMED_OUT, hung.reason_text(None)
    assert hung.active_at_end is not None and hung.active_at_end.qualname == "test_hangs"


def test_python_tests_are_timed_against_the_extension(project, python_adapter, suite):
    root = project("calc_ext")
    tests = suite(TESTS, "test__calc.py")
    timed = python_adapter(root).tests.benchmark(BenchmarkRequest(tests, runs=2,
                                                                  timeout_s=180))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert len(timed.times_ms) == 2 and timed.warmup_runs == 1
    assert "extension" in timed.includes
    # A module that fails to load is no time of the extension's tests.
    source = root / TARGET
    source.write_text(source.read_text().replace("return made;",
                                                 "return NULL;"))
    timed = python_adapter(root).tests.benchmark(BenchmarkRequest(tests, runs=2,
                                                                  timeout_s=180))
    assert timed.outcome is la.Outcome.INFRASTRUCTURE_FAILURE
    assert "did not load" in timed.reason and timed.times_ms == ()


@needs_mull
def test_a_mutation_run_of_python_tests(project, python_adapter, suite):
    root = project("calc_ext")
    adapter = python_adapter(root)
    with adapter.mutations.open_session(_mull.ENGINE, workers=2) as session:
        run = session.run(MutationRequest(target=TARGET, suite=suite(TESTS, "test__calc.py"),
                                          attribution="all"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    # A request without timeout_journal journals its Python tests.
    assert run.settings["timeout_journal"].value is True
    by = {(m.key.operator, m.key.line): m for m in run.mutants}
    negated = by[("cxx_lt_to_ge", CLAMP_IF_LOW)]
    assert negated.status == KILLED_STATUS
    assert sorted(negated.killers) == ["test__calc.test_clamp_below",
                                       "test__calc.test_clamp_inside"]
    # made != NULL: the module's init returns NULL without an error.
    broken_init = by[("cxx_eq_to_ne", INIT_CHECK)]
    assert broken_init.row["native_status"] == "load_error"
    assert broken_init.classification == Classification(
        NON_VIABLE_STATUS, MutationClass.DETECTED, NON_VIABLE)
    signs = [m for m in run.mutants if SIGN[0] <= m.key.line <= SIGN[1]]
    assert signs and all(m.row.get("not_executed") for m in signs)
    tokens, unmatched = adapter.kill_tokens(run.to_kill_matrix(), {
        "test__calc.test_clamp_below", "test__calc.test_clamp_inside"})
    assert unmatched == ()
    assert f"K:{negated.row['id']}" in tokens["test__calc.test_clamp_below"]


@needs_mull
def test_a_mutation_run_without_the_timeout_journal_runs_its_tests_without_it(
        project, python_adapter, suite, monkeypatch):
    """An explicit timeout_journal False reaches every run of the Python
    tests, which then keep no journal, and the run records it."""
    from langadapter.c import _extension

    seen = []
    run_tests = _extension.Harness.run

    def recorded(self, request, **options):
        if "mutant" in options:  # a mutant's run, not the origin's
            seen.append(request.timeout_journal)
        return run_tests(self, request, **options)

    monkeypatch.setattr(_extension.Harness, "run", recorded)
    with python_adapter(project("calc_ext")).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target=TARGET, suite=suite(TESTS, "test__calc.py"),
                                          catalogue=Catalogue(operators=("cxx_lt_to_ge",)),
                                          timeout_journal=False))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert run.settings["timeout_journal"].value is False
    assert seen and set(seen) == {False}


#: A test that never returns under clamp's x < lo turned x >= lo.
WAITING_TESTS = """\
from calcext import _calc


def test_clamp_waits_for_the_middle():
    while _calc.clamp(5, 0, 10) != 5:
        pass
"""


@needs_mull
def test_a_mutant_the_wall_clock_cuts_in_python_tests_is_untested(project, python_adapter,
                                                                   suite):
    """With Python tests too, a mutant whose tests the run's wall clock ends
    before one failed is untested, not a timeout."""
    with python_adapter(project("calc_ext")).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target=TARGET, suite=suite(WAITING_TESTS, "test__calc.py"),
                                          catalogue=Catalogue(operators=("cxx_lt_to_ge",)),
                                          per_mutant_timeout=600, wall_clock_limit_s=60))
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason
    cut = {(m.key.operator, m.key.line): m for m in run.mutants}[("cxx_lt_to_ge", CLAMP_IF_LOW)]
    assert cut.status == UNTESTED_STATUS


def test_the_source_operations_of_python_tests_are_pythons(project, python_adapter):
    """In extension mode merging, syntax repair, skip removal and load
    repair are Python's, through the public source operations."""
    sources = python_adapter(project("calc_ext")).sources
    for name in ("merge_sources", "salvage_syntax", "strip_module_level_skips",
                 "prune_collection_breakers"):
        assert sources.supports(name), name
    merged = sources.merge_sources(TESTS, TESTS.replace("test_clamp_", "test_more_"),
                                   separator="# second")
    assert merged.status == "applied", merged.detail
    assert [line for line in merged.source.splitlines() if line.startswith("def ")] == [
        "def test_clamp_below():", "def test_clamp_inside():", "def test_more_below():",
        "def test_more_inside():"]
    broken = TESTS.replace("== 0\n", "== 0 +\n")
    salvaged = sources.salvage_syntax(broken)
    assert salvaged.status == "applied", salvaged.detail
    compile(salvaged.source, "test__calc.py", "exec")
    assert "def test_clamp_inside():" in salvaged.source
    stripped = sources.strip_module_level_skips(
        "import pytest\n\npytest.skip('later', allow_module_level=True)\n\n" + TESTS)
    assert stripped.status == "applied" and "pytest.skip(" not in stripped.source
    pruned = sources.prune_collection_breakers("import not_installed_anywhere\n" + TESTS,
                                               timeout_s=180)
    assert pruned.status == "applied", pruned.detail
    assert "not_installed_anywhere" not in pruned.source
    assert "def test_clamp_inside():" in pruned.source


def test_a_pooled_kill_of_python_tests_goes_to_its_suite(project, python_adapter):
    adapter = python_adapter(project("calc_ext"))
    killer = la.TestId(file="test__calc_001.py", qualname="test_clamp_below")
    text = adapter.test_id_text(killer)
    matrix = {"mutants": [{"id": "m1", "killers": [text],
                           "killing_tests": [dataclasses.asdict(killer)]}]}
    assert adapter.pool_kills(matrix, {"test__calc_001.py": 1}, {1: {text}}) == {
        1: {text: {"K:m1"}}}


@needs_mull
def test_a_real_pools_python_kills_go_to_the_suite_whose_test_made_them(
        project, python_adapter, tmp_path):
    """A staged pool of two Python suites whose tests share their names, run
    through Mull: each kill goes to the suite whose file holds the killing
    test."""
    from test_mutation import assert_pooled_kills

    adapter = python_adapter(project("calc_ext"))
    second = TESTS.replace("_calc.clamp(5, 0, 10) == 5", "_calc.clamp(15, 0, 10) == 10")
    pool = adapter.stage_pool([PoolMember("s1", "test__calc.py", TESTS),
                               PoolMember("s2", "test__calc.py", second)])
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    with adapter.mutations.open_session(_mull.ENGINE, workers=2) as session:
        run = session.run(MutationRequest(target=TARGET, suite=pool.write(str(pool_dir)),
                                          attribution="all"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert_pooled_kills(adapter, run, {"test__calc_s1.py": 1, "test__calc_s2.py": 2},
                        ("test_clamp_below", "test_clamp_inside"))


@needs_mull
def test_a_mutant_that_crashes_the_tests_is_detected(project, python_adapter, suite, tmp_path,
                                                     monkeypatch):
    # The system temp directory of these runs, which they leave empty.
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    root = project("calc_ext")
    tests = suite("""\
from calcext import _calc


def test_head_without_flag():
    assert _calc.head(False) == -1


def test_head_with_flag():
    assert _calc.head(True) == 7
""", "test__calc.py")
    runs = {}
    for attribution in ("first", "all"):
        with python_adapter(root).mutations.open_session(_mull.ENGINE) as session:
            runs[attribution] = session.run(MutationRequest(
                target=TARGET, suite=tests, attribution=attribution,
                catalogue=Catalogue(operators=("cxx_eq_to_ne",))))
        assert runs[attribution].outcome is la.Outcome.COMPLETED, runs[attribution].reason
    # table != NULL reads table[0] of a NULL table: the test process ends by
    # SIGSEGV while test_head_without_flag runs.
    [first] = [m for m in runs["first"].mutants if m.key.line == HEAD_TEST]
    assert first.row["native_status"] == "crashed"
    assert first.classification.subcategory == "no_named_test"
    assert first.row["signal"] == 11
    assert first.row["crashed_in"] == ["test__calc.test_head_without_flag"]
    # With every killer asked for, the tests after the crash run again in a
    # new process: test_head_with_flag reads -1 for 7 and kills the mutant.
    [every] = [m for m in runs["all"].mutants if m.key.line == HEAD_TEST]
    assert every.row["native_status"] == "killed"
    assert every.killers == ("test__calc.test_head_with_flag",)
    assert every.row["crashed_in"] == ["test__calc.test_head_without_flag"]
    assert "killers_incomplete" not in every.row
    # The Python adapter retains a crashed run's scratch, with its receipt
    # outside it. Neither stays.
    del runs, first, every, session
    gc.collect()
    assert sorted(system_temp.iterdir()) == []


def test_c_tests_of_an_extension_project_do_not_take_the_compiler_for_python(
        project, adapter, suite):
    """c_tests c on a project whose setup.py declares an extension: its
    setuptools build makes a Python extension for Python tests, so the run
    misses setup with a reason that names c_build_kind."""
    c = adapter(project("calc_ext"), c_tests="c")
    run = c.run_suite(SuiteRunRequest(suite(
        '#include "utest.h"\n\nUTEST(calc, nothing) {\n    ASSERT_TRUE(1);\n}\n\n'
        "UTEST_MAIN()\n", "test__calc.c"), timeout_s=180, coverage_target=TARGET))
    assert run.outcome is la.Outcome.MISSING_SETUP, run.reason_text(None)
    assert "c_build_kind names it" in run.reason_text(None)


def test_c_tests_set_for_an_extension_project_are_c_tests(project, adapter):
    """c_tests c on a project whose setup.py declares an extension: the
    tests are C's, not the Python tests discovery would choose."""
    c = adapter(project("calc_ext"), c_tests="c")
    facts = c.language_facts()
    assert not c._extension_mode
    assert (facts.test_framework, facts.code_fence, facts.test_language) == (
        "utest.h", "c", None)


def test_the_target_finder_comes_before_any_import(project, python_adapter, tmp_path):
    import subprocess

    from langadapter.c import _extension

    root = project("calc_ext")
    harness = _extension.Harness(python_adapter(root), tmp_path / "harness")
    harness.config.write_text(f"module\tcalcext._calc\nextension\tx.so\n"
                              f"report\t{tmp_path / 'report.txt'}\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / "sitecustomize.py").write_text("import os\nos.environ['OTHER_SITE'] = 'ran'\n")
    env = dict(harness.python.context.environment)
    env["PYTHONPATH"] = env["PYTHONPATH"] + f":{other}"
    probe = ("import os, sys; print(type(sys.meta_path[0]).__name__, "
             "os.environ.get('OTHER_SITE'), 'random' in sys.modules)")
    done = subprocess.run([sys.executable, "-c", probe], env={**__import__("os").environ, **env},
                          capture_output=True, text=True, check=True)
    assert done.stdout.split() == ["_TargetFinder", "ran", "False"]


@needs_mull
def test_a_mutant_that_breaks_the_runner_is_detected(project, python_adapter, tmp_path):
    root = project("calc_ext")
    suite_dir = tmp_path / "runner_suite"
    suite_dir.mkdir()
    (suite_dir / "test__calc.py").write_text(TESTS)
    # The runner itself uses the target, as pytest uses CPython's bisect.
    (suite_dir / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    from calcext import _calc\n"
        "    if _calc.clamp(-5, 0, 10) != 0:\n"
        "        raise RuntimeError('the runner could not order its tests')\n")
    tests = la.Suite(str(suite_dir), ("test__calc.py",), ("conftest.py",))
    with python_adapter(root).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(
            target=TARGET, suite=tests, catalogue=Catalogue(operators=("cxx_lt_to_ge",))))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    negated = next(m for m in run.mutants if m.key.line == CLAMP_IF_LOW)
    assert negated.row["native_status"] == "runner_error"
    assert negated.classification.subcategory == "no_named_test"
    assert "the runner could not order its tests" in negated.row["reason"]
    assert run.summary["runner_error"] == sum(
        1 for m in run.mutants if m.row["native_status"] == "runner_error") >= 1


def test_python_tests_of_an_extension_are_screened_for_reading_or_copying_it(project,
                                                                           python_adapter):
    root = project("calc_ext")
    adapter = python_adapter(root)
    assert adapter.sources.supports("analyze_validity")
    target = adapter.target_identity(TARGET)
    assert (target.module_name, target.language, Path(target.source_path).name) == (
        "calcext._calc", "python", "_calc.c")
    assert target.policy_version == PythonAdapter.validity_policy_version + "+c-extension-v1"
    with pytest.raises(InvalidRequest):
        adapter.target_identity("setup.py")
    report = adapter.analyze_validity(TESTS, target)
    assert (report.status, report.items, report.policy_version) == (
        "completed", (), target.policy_version)
    reads = TESTS + '\n\ndef test_reads():\n    assert "clamp" in open("calcext/_calc.c").read()\n'
    absolute = reads.replace('"calcext/_calc.c"', repr(target.source_path))
    named = reads.replace('"calcext/_calc.c"', '"_calc.c"')
    copied = TESTS + f"\n\nSOURCE = {(root / TARGET).read_text()!r}\n"
    for source, rule, test in ((reads, "target_source_read", "test_reads"),
                               (absolute, "target_source_read", "test_reads"),
                               (named, "target_source_read", "test_reads"),
                               (copied, "copied_target", None)):
        report = adapter.analyze_validity(source, target)
        assert [(f.rule_id, f.enclosing_test) for f in report.items] == [(rule, test)], source
        assert {f.policy_version for f in report.items} == {target.policy_version}
    assert [f.rule_id for f in adapter.screen_source(reads, target).items] == ["target_literal"]


def test_pythons_own_policy_is_unchanged(project):
    root = project("calc_ext")
    (root / "calc.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
    python = PythonAdapter(la.ProjectContext(root=str(root),
                                             interpreter=la.Interpreter(path=sys.executable)))
    identity = python.target_identity("calc.py")
    # Python's own policy, whatever its version: the C package changes nothing.
    assert (identity.module_name, identity.language, identity.policy_version) == (
        "calc", "python", PythonAdapter.validity_policy_version)
    with pytest.raises(InvalidRequest, match="cannot derive an importable module name"):
        python.target_identity(TARGET)
    relative = 'import calc\n\n\ndef test_reads():\n    assert open("calc.py").read()\n'
    absolute = relative.replace('"calc.py"', repr(identity.source_path))
    assert [f.rule_id for f in python.screen_source(relative, identity).items] == [
        "python_source_read", "target_literal"]
    # A read of the target by a relative or an absolute path is the test's.
    for source in (relative, absolute):
        report = python.analyze_validity(source, identity)
        assert ([(f.rule_id, f.enclosing_test) for f in report.items],
                report.policy_version) == ([("target_source_read", "test_reads")],
                                           PythonAdapter.validity_policy_version)


def test_an_extensions_timing_through_a_container_says_its_runner_runs_there(
        project, python_adapter, suite, monkeypatch):
    """A timing of an extension's Python tests through a container's executor
    states that the runner runs in the container, with the container
    runtime's execution overhead."""
    import subprocess

    class Container:
        def run(self, argv, **options):
            return subprocess.CompletedProcess(argv, 1, "", "")

    tests = suite(TESTS, "test__calc.py")
    adapter = python_adapter(project("calc_ext"))
    monkeypatch.setattr(adapter, "_executor", Container)
    timed = adapter.tests.benchmark(BenchmarkRequest(tests, runs=1, timeout_s=60))
    assert timed.outcome is la.Outcome.MISSING_SETUP, timed.reason
    assert timed.includes.startswith("the runner's process in the container"), timed
    assert "the container runtime's execution overhead" in timed.includes
