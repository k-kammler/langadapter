"""The Python adapter's operations through the container executor in a real
Apptainer instance, with pinned test tools from an offline wheelhouse and the
work directory bound at the same path. Runs where ``LANGADAPTER_TEST_SIF`` names an
image, ``LANGADAPTER_TEST_WHEELHOUSE`` a directory of wheels (pytest,
coverage, cosmic-ray and dependencies) and apptainer is installed. The
interpreter is ``LANGADAPTER_TEST_CONTAINER_PYTHON`` or the conda environment
the image's ``TESTBED_NAME`` names."""
import os
import shutil
import subprocess
import sys
import uuid

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.outcomes import Outcome
from langadapter.python._runs import INCLUDES_CONTAINER
from langadapter.python.project import _setup_test_env
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import (
    PASSED, BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest,
)
from langadapter.suites import TestId

SIF = os.environ.get("LANGADAPTER_TEST_SIF")
WHEELHOUSE = os.environ.get("LANGADAPTER_TEST_WHEELHOUSE")
pytestmark = pytest.mark.skipif(
    not (SIF and WHEELHOUSE and shutil.which("apptainer")),
    reason="needs LANGADAPTER_TEST_SIF, LANGADAPTER_TEST_WHEELHOUSE and "
           "apptainer")

TARGET = "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n"
TESTS = '''import importlib
import random

import sign as module
from sign import sign


def test_positive():
    assert sign(2) == 1


def test_negative():
    assert sign(-2) == -1


def test_seed_zero():
    assert random.random() == 0.8444218515250481


def test_reload():
    importlib.reload(module)
'''
#: The conda environments of the benchmark images.
_CONDA_ENVS = "/home/swe-bench/miniconda3/envs"


def _apptainer(*argv):
    return subprocess.run(["apptainer", *argv], capture_output=True,
                          text=True, timeout=900)


@pytest.fixture(scope="module")
def instance(tmp_path_factory):
    """A running instance with pytest and coverage, and its interpreter."""
    zone = tmp_path_factory.mktemp("zone")
    os.chmod(zone, 0o777)
    wheels = zone / "wheels"
    shutil.copytree(WHEELHOUSE, wheels)
    name = f"lbtest_{uuid.uuid4().hex[:8]}"
    started = _apptainer("instance", "start", "--writable-tmpfs",
                         "-B", f"{zone}:{zone}", SIF, name)
    assert started.returncode == 0, started.stderr
    try:
        python = os.environ.get("LANGADAPTER_TEST_CONTAINER_PYTHON")
        if not python:
            testbed = _apptainer("exec", f"instance://{name}", "printenv",
                                 "TESTBED_NAME").stdout.strip()
            python = f"{_CONDA_ENVS}/{testbed}/bin/python"
        installed = _apptainer(
            "exec", f"instance://{name}", python, "-m", "pip", "install",
            "-q", "--no-index", "--find-links", str(wheels),
            "pytest==8.1.1", "coverage==7.6.8", "cosmic-ray==8.4.1")
        assert installed.returncode == 0, installed.stderr
        yield name, python, zone
    finally:
        _apptainer("instance", "stop", name)


def _context(instance, root):
    name, python, zone = instance
    return lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=python),
        test_framework=lb.TestFrameworkConfig("isolated"),
        execution=lb.ContainerExecution(container=name, work_dir=str(zone),
                                        runner="apptainer"))


def test_the_capability_report_probes_the_container(instance):
    _name, _python, zone = instance
    report = PythonAdapter(_context(instance, zone)).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED, report.reason
    found = {tool.name: tool.found for tool in report.tools}
    assert found["pytest"] == "8.1.1" and found["coverage"] == "7.6.8"
    assert report.capability("suite_runs").available
    assert report.runtime.startswith("CPython 3.")


def test_the_public_operations_run_in_the_container(instance, tmp_path, carry_instrument):
    """Public suite runs, per-test evidence and benchmarks run in the
    container, with the suite in the work directory; a copy that carries the
    determinism instrument seeds its tests."""
    _name, _python, zone = instance
    root = zone / "pproj"
    root.mkdir()
    (root / "sign.py").write_text(TARGET, encoding="utf-8")
    tests = zone / "ptests"
    tests.mkdir()
    (tests / "test_sign.py").write_text(TESTS, encoding="utf-8")
    adapter = PythonAdapter(_context(instance, root))
    suite = lb.Suite(str(tests), ("test_sign.py",))
    seeded = zone / "ctests"
    seeded.mkdir()
    (seeded / "test_sign.py").write_text(TESTS, encoding="utf-8")
    carry_instrument(seeded)
    carried = lb.Suite(str(seeded), ("test_sign.py",), ("conftest.py",))
    target = lb.FileIdentity.of(root / "sign.py", root)

    run = adapter.run_suite(SuiteRunRequest(
        suite=carried, timeout_s=600, coverage_target="sign.py",
        record_leaks=True))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.counts.passed == 4 and run.coverage.line_percent == 100.0
    assert run.coverage.target == target == run.identity.target
    assert [(str(leak.test), leak.rebound) for leak in run.leaks] == [
        ("test_sign.py::test_reload", ("sign",))]
    assert not run.failing_tests()
    assert run.identity.execution.kind == "container"

    # As delivered: the seed-dependent test fails, the rest pass.
    delivered = adapter.run_suite(SuiteRunRequest(
        suite=suite, timeout_s=600, session_seed=1))
    assert [str(t) for t in delivered.failing_tests()] == [
        "test_sign.py::test_seed_zero"], delivered.output
    assert sorted(t.test.qualname for t in delivered.tests
                  if t.outcome == PASSED) == [
        "test_negative", "test_positive", "test_reload"]

    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite=carried, target="sign.py", timeout_s=600))
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    assert {str(e.test) for e in evidence.tests} == {
        "test_sign.py::test_positive", "test_sign.py::test_negative",
        "test_sign.py::test_seed_zero", "test_sign.py::test_reload"}
    assert all(entry.time_ms > 0 for entry in evidence.tests)
    assert evidence.coverage.target == target

    # A processor the instance may run on: it inherits this process's set.
    cpu = str(min(os.sched_getaffinity(0)))
    timing = adapter.benchmark(BenchmarkRequest(
        suite=carried, runs=2, warmup_runs=1, timeout_s=600,
        cpus=cpu))
    assert timing.outcome is Outcome.COMPLETED, timing.reason
    assert len(timing.times_ms) == 2 and timing.min_ms <= timing.max_ms
    assert timing.cpus == cpu
    assert timing.includes == INCLUDES_CONTAINER

    # A suite the container cannot see is refused before anything runs.
    (tmp_path / "test_sign.py").write_text(TESTS, encoding="utf-8")
    with pytest.raises(InvalidRequest) as refused:
        adapter.run_suite(SuiteRunRequest(
            suite=lb.Suite(str(tmp_path), ("test_sign.py",)), timeout_s=600))
    assert refused.value.field == "suite.root"


@pytest.mark.mutation_tool
def test_a_mutation_session_runs_in_the_container(instance):
    """Mutation workers run in the container, recorded as the execution."""
    _name, _python, zone = instance
    root = zone / "mproj"
    root.mkdir()
    (root / "sign.py").write_text(TARGET, encoding="utf-8")
    tests = zone / "mtests"
    tests.mkdir()
    (tests / "test_sign.py").write_text(
        "from sign import sign\n\n\n"
        "def test_positive():\n    assert sign(2) == 1\n\n\n"
        "def test_negative():\n    assert sign(-2) == -1\n",
        encoding="utf-8")
    adapter = PythonAdapter(_context(instance, root))
    suite = lb.Suite(str(tests), ("test_sign.py",))
    catalogue = Catalogue(operators=("core/ReplaceComparisonOperator_Gt_Lt",))
    with adapter.open_mutation_session() as session:
        assert session.outcome is Outcome.COMPLETED, session.reason
        # A target is a file under the project root in the container.
        for target in ("missing.py", str(zone / "mtests" / "test_sign.py")):
            with pytest.raises(InvalidRequest):
                session.run(MutationRequest(target=target, suite=suite,
                                            catalogue=catalogue))
        run = session.run(MutationRequest(
            target="sign.py", suite=suite, attribution="all",
            catalogue=catalogue))
    assert run.outcome is Outcome.COMPLETED, run.reason
    (mutant,) = run.mutants
    assert set(mutant.killing_tests) == {
        TestId("test_sign.py", "test_positive"),
        TestId("test_sign.py", "test_negative")}
    assert run.identity.observation.execution.kind == "container"
    assert run.identity.observation.target.path == "sign.py"


@pytest.mark.mutation_tool
@pytest.mark.parametrize("staged, journal", [
    (True, None), (False, None), (True, False), (False, False)])
def test_a_container_run_names_the_test_a_mutant_hangs(instance, staged,
                                                       journal):
    """A looping mutant keeps its timeout verdict with the hung test as
    evidence. The container journal pairs with the verdict by the module's
    text (here with CRLF line ends). The instrument's setup file writes the
    journal, or without it the adapter's plugin does. With the journal off
    the verdict has no reading."""
    _name, _python, zone = instance
    tag = f"{'staged' if staged else 'plain'}_{journal}"
    root = zone / f"hproj_{tag}"
    root.mkdir()
    (root / "countdown.py").write_bytes(
        b"def countdown(n):\r\n    while n > 0:\r\n"
        b"        n = n - 1\r\n    return n\r\n")
    tests = zone / f"htests_{tag}"
    tests.mkdir()
    (tests / "test_countdown.py").write_text(
        "from countdown import countdown\n\n\n"
        "def test_countdown():\n    assert countdown(3) == 0\n",
        encoding="utf-8")
    if staged:
        _setup_test_env(PythonAdapter(_context(instance, root)), str(tests), str(root))
    adapter = PythonAdapter(_context(instance, root))
    suite = lb.Suite(str(tests), ("test_countdown.py",))
    catalogue = Catalogue(operators=("core/ReplaceBinaryOperator_Sub_Add",))
    with adapter.open_mutation_session() as session:
        assert session.outcome is Outcome.COMPLETED, session.reason
        run = session.run(MutationRequest(
            target="countdown.py", suite=suite, attribution="all",
            catalogue=catalogue, per_mutant_timeout=10,
            timeout_journal=journal))
    assert run.outcome is Outcome.COMPLETED, run.reason
    (mutant,) = run.mutants
    assert mutant.status == "timeout", mutant.row
    readings = [e for e in mutant.evidence if e.kind == "timeout"]
    if journal is False:
        assert readings == []
        return
    (timeout,) = readings
    assert timeout.label == "hang"
    assert [test.split("::")[-1] for test in timeout.tests] == [
        "test_countdown"]


#: The first test changes a module setting and the second needs it unchanged,
#: so the suite passes only with the per-test restore. No test reaches
#: ``x - 2``, and ``n * 2`` made ``n + 2`` fails the first test.
FLAG_TARGET = ("FLAG = 0\n\n\n"
               "def flag():\n    return FLAG\n\n\n"
               "def unused(x):\n    return x - 2\n\n\n"
               "def double(n):\n    return n * 2\n")
FLAG_TESTS = ("import flagged\nfrom flagged import double\n\n\n"
              "def test_a_sets_the_flag():\n    flagged.FLAG = 1\n"
              "    assert double(3) == 6\n\n\n"
              "def test_b_reads_the_flag():\n    assert flagged.flag() == 0\n")


@pytest.mark.mutation_tool
@pytest.mark.parametrize("workers", [1, 2])
def test_a_local_and_a_container_run_agree_on_a_suite_changing_the_module(
        instance, tmp_path, workers, carried_instrument):
    """A suite delivered with the instrument, after its import shim, has
    the module restored before each test locally as in the container: one
    whose first test changes the module measures the same verdicts and
    killers on both, one worker in place and two in clones. Without the
    restore the second test would fail under every mutant and kill the one
    no test reaches. No run places the instrument itself."""
    _name, _python, zone = instance
    catalogue = Catalogue(operators=("core/ReplaceBinaryOperator_Sub_Add",
                                     "core/ReplaceBinaryOperator_Mul_Add"))
    options = ({"workers": 2, "distributor": "http"} if workers > 1
               else {"workers": 1})
    verdicts = {}
    for where, base in (("container", zone), ("local", tmp_path)):
        root = base / f"fproj_{where}_{workers}"
        tests = base / f"ftests_{where}_{workers}"
        root.mkdir()
        tests.mkdir()
        (root / "flagged.py").write_text(FLAG_TARGET, encoding="utf-8")
        (tests / "test_flagged.py").write_text(FLAG_TESTS, encoding="utf-8")
        context = (_context(instance, root) if where == "container"
                   else lb.ProjectContext(
                       root=str(root),
                       interpreter=lb.Interpreter(path=sys.executable)))
        _setup_test_env(PythonAdapter(context), str(tests), str(root))
        conftest = tests / "conftest.py"
        conftest.write_text(conftest.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n"
                            + carried_instrument, encoding="utf-8")
        with PythonAdapter(context).open_mutation_session(**options) as session:
            assert session.outcome is Outcome.COMPLETED, session.reason
            run = session.run(MutationRequest(
                target="flagged.py",
                suite=lb.Suite(str(tests), ("test_flagged.py",)),
                attribution="all", catalogue=catalogue))
        assert run.outcome is Outcome.COMPLETED, (where, run.reason)
        verdicts[where] = {
            m.key.line: (m.status, sorted(str(t) for t in m.killing_tests))
            for m in run.mutants}
    assert verdicts["local"] == verdicts["container"]
    assert verdicts["local"] == {
        9: ("survived", []),
        13: ("killed", [str(TestId("test_flagged.py",
                                      "test_a_sets_the_flag"))])}
