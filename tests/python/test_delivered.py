"""Generated-suite portability through the public, context-bound operation."""
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from langadapter.context import Interpreter, ProjectContext
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import SuiteRunRequest
from langadapter.suites import Suite

#: The determinism instrument as a suite delivered with it carries it.
CARRIED_INSTRUMENT = Path(__file__).with_name("carried_instrument.txt").read_text(
    encoding="utf-8")


def _fixture(tmp_path, files):
    root = tmp_path / "project"
    suite = root / "tests"
    suite.mkdir(parents=True)
    for name, text in files.items():
        (suite / name).write_text(text, encoding="utf-8")
    adapter = PythonAdapter(ProjectContext(
        str(root), Interpreter(path=sys.executable)))
    # A suite delivered with the instrument: the shim, then the instrument.
    prepared = adapter.prepare_suite(str(suite))
    assert prepared.outcome == Outcome.COMPLETED
    conftest = suite / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + CARRIED_INSTRUMENT,
                        encoding="utf-8")
    request = SuiteRunRequest(
        Suite(str(suite), tuple(files), prepared.setup_files),
        timeout_s=60, session_seed=1, isolation_config=True)
    return adapter, request, suite


def test_carried_instrument_is_removed_without_changing_the_input(tmp_path):
    adapter, request, suite = _fixture(tmp_path, {"test_state.py":
        "import random\n"
        "def test_seed():\n    assert random.random() == random.Random(0).random()\n"})
    before = {p.name: p.read_bytes() for p in suite.iterdir() if p.is_file()}
    run = adapter.run_as_delivered(request)
    assert run.outcome == Outcome.TEST_FAILURES
    assert run.counts.failed == 1
    assert str(run.tests[0].test) == "test_state.py::test_seed"
    assert run.identity.instrument is None and run.side_effects.instrument is None
    assert run.side_effects.session_seed == 1
    assert {p.name: p.read_bytes() for p in suite.iterdir() if p.is_file()} == before


def test_each_file_has_its_own_session_and_import_setup_location(tmp_path):
    files = {name: "import random\nimport probe\n"
        "def test_first():\n"
        "    assert probe.VALUE == 7\n"
        "    assert random.random() == random.Random(1).random()\n"
        for name in ("test_a.py", "test_b.py")}
    adapter, request, suite = _fixture(tmp_path, files)
    (suite.parent / "src").mkdir()
    (suite.parent / "src" / "probe.py").write_text("VALUE = 7\n", encoding="utf-8")
    setup = suite / "conftest.py"
    setup.write_text("import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))\n"
        + setup.read_text(encoding="utf-8"), encoding="utf-8")
    run = adapter.run_as_delivered(request)
    assert run.outcome == Outcome.COMPLETED, run.reason
    assert run.counts.passed == 2
    assert [test.test.file for test in run.tests] == list(files)


def test_source_replacement_is_measured_but_never_written_to_the_suite(tmp_path):
    adapter, request, suite = _fixture(tmp_path, {"test_a.py":
        "def test_a():\n    assert False\n"})
    replacement = "def test_a():\n    assert True\n"
    original = (suite / "test_a.py").read_bytes()
    run = adapter.run_as_delivered(request, sources={"test_a.py": replacement})
    assert run.outcome == Outcome.COMPLETED
    assert (suite / "test_a.py").read_bytes() == original
    identity = next(f for f in run.identity.suite if f.path == "test_a.py")
    assert identity.sha256 == hashlib.sha256(replacement.encode()).hexdigest()


def test_reverse_order_preserves_parameterized_outcomes(tmp_path):
    adapter, request, _ = _fixture(tmp_path, {"test_order.py":
        "import pytest\nSTATE = []\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_first(x):\n    STATE.append(x)\n"
        "def test_last():\n    assert STATE == [1, 2]\n"})
    forward = adapter.run_as_delivered(request)
    backward = adapter.run_as_delivered(replace(request, reverse_order=True))
    assert forward.outcome == Outcome.COMPLETED
    assert backward.outcome == Outcome.TEST_FAILURES
    assert [t.test.params for t in forward.tests] == ["1", "2", None]
    assert backward.failing_tests()[0].qualname == "test_last"


def test_collection_failure_is_not_a_decided_red_suite(tmp_path):
    adapter, request, _ = _fixture(tmp_path, {"test_a.py":
        "raise RuntimeError('collection failed')\n"})
    run = adapter.run_as_delivered(request)
    assert run.outcome == Outcome.COLLECTION_FAILED
    assert run.tests == () and "collection failed" in run.reason


def test_delivery_requires_a_seed_and_refuses_unlisted_replacements(tmp_path):
    adapter, request, _ = _fixture(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})
    with pytest.raises(InvalidRequest, match="session_seed"):
        adapter.run_as_delivered(replace(request, session_seed=None))
    with pytest.raises(InvalidRequest, match="sources"):
        adapter.run_as_delivered(request, sources={"../elsewhere.py": "pass\n"})


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.0])
def test_a_seed_outside_python_s_hash_seed_range_is_refused(tmp_path, seed):
    """Python refuses a PYTHONHASHSEED above 4294967295 at start, so such a
    seed is an invalid request, not a run that fails."""
    adapter, request, _ = _fixture(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})
    with pytest.raises(InvalidRequest, match="session_seed: must be an integer from 0 to 4294967295"):
        adapter.run_as_delivered(replace(request, session_seed=seed))
    replace(request, session_seed=2**32 - 1).validate()


@pytest.mark.parametrize("spent, given, outcome, reason", [
    ((20, 5), [60, 40], Outcome.COMPLETED, ""),
    ((20, 50), [60, 40], Outcome.TIMED_OUT, "test_b.py: the run reached the timeout of 60 s"),
    ((60, 5), [60], Outcome.TIMED_OUT, "the timeout of 60 s ended before test_b.py ran"),
])
def test_the_timeout_bounds_the_whole_run(tmp_path, monkeypatch, spent, given, outcome, reason):
    """Each file runs in what is left of the request's timeout, and its
    timeout is reported as the run's. A run whose time is spent ends timed
    out before the next file. The run records the toolchain its file runs
    recorded."""
    from types import SimpleNamespace

    from langadapter.context import LocalExecution
    from langadapter.outcomes import ToolchainIdentity
    from langadapter.python import _runs
    from langadapter.runs import RunCounts, SuiteRun

    toolchain = ToolchainIdentity("CPython", "fixture")

    zone = tmp_path / "zone"
    zone.mkdir()
    adapter, request, _ = _fixture(tmp_path, {name: "def test_x():\n    pass\n"
                                              for name in ("test_a.py", "test_b.py")})
    adapter = PythonAdapter(replace(adapter.context, execution=LocalExecution(str(zone))))
    clock = SimpleNamespace(now=100.0)
    limits = []

    def run_suite(_adapter, file_request, **_options):
        # The file takes its time of the clock, and past its limit times out.
        limits.append(file_request.timeout_s)
        took = spent[len(limits) - 1]
        clock.now += min(took, file_request.timeout_s)
        timed_out = took > file_request.timeout_s
        return SuiteRun(
            Outcome.TIMED_OUT if timed_out else Outcome.COMPLETED,
            f"the runner reached the timeout of {file_request.timeout_s:g} s"
            if timed_out else "", RunCounts(passed=0 if timed_out else 1), (), (),
            None if timed_out else took, "", None, SimpleNamespace(toolchain=toolchain))

    monkeypatch.setattr(_runs, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(_runs, "run_suite", run_suite)
    run = adapter.run_as_delivered(request)
    assert limits == given
    assert (run.outcome, run.reason) == (outcome, reason)
    assert run.identity.toolchain is toolchain


def test_a_run_whose_time_ends_before_its_first_file_counts_nothing(tmp_path, monkeypatch):
    """A run whose timeout ends before its first file runs observed no
    count and no time, so each is absent, not zero."""
    from types import SimpleNamespace

    from langadapter.context import LocalExecution
    from langadapter.python import _runs
    from langadapter.runs import RunCounts

    zone = tmp_path / "zone"
    zone.mkdir()
    adapter, request, _ = _fixture(tmp_path, {"test_a.py": "def test_x():\n    pass\n"})
    adapter = PythonAdapter(replace(adapter.context, execution=LocalExecution(str(zone))))
    # The first reading sets the deadline, every later one lies past it.
    times = iter([100.0])
    monkeypatch.setattr(_runs, "time", SimpleNamespace(monotonic=lambda: next(times, 1000.0)))
    run = adapter.run_as_delivered(request)
    assert (run.outcome, run.reason) == (
        Outcome.TIMED_OUT, "the timeout of 60 s ended before test_a.py ran")
    assert run.counts == RunCounts() and run.wall_time_s is None


def test_a_staged_run_in_a_container_matches_its_paths_case_exactly(tmp_path, monkeypatch):
    """A container's paths are case-sensitive, also on a Windows host, so
    only the staged directory's own spelling reads as the original. A
    local run on Windows ignores case."""
    import os
    from types import SimpleNamespace

    from langadapter.context import ContainerExecution
    from langadapter.python import _runs

    class WindowsHost:
        name = "nt"

        def __getattr__(self, attribute):
            return getattr(os, attribute)

    monkeypatch.setattr(_runs, "os", WindowsHost())
    text = "/work/stage/test_a.py and /WORK/STAGE/test_b.py"
    container = ProjectContext("/project", Interpreter(path="/usr/bin/python3"),
                               execution=ContainerExecution("selected", work_dir=str(tmp_path),
                                                            helper_python="/control/python"))
    local = ProjectContext(str(tmp_path), Interpreter(path=sys.executable))
    read = {}
    for name, context in (("container", container), ("local", local)):
        runner = SimpleNamespace(adapter=SimpleNamespace(context=context),
                                 container=_runs.in_container(context),
                                 path_pairs=(("/work/stage", "/project/tests"),))
        read[name] = _runs._SuiteRunner._original_text(runner, text)
    assert read == {"container": "/project/tests/test_a.py and /WORK/STAGE/test_b.py",
                    "local": "/project/tests/test_a.py and /project/tests/test_b.py"}
