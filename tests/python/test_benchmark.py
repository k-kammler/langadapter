"""Python adapter benchmarks with real pytest: measured repetitions after
discarded warm-ups, nothing written, the given placement, and an early end
that keeps the passed repetitions without statistics."""
import os
import shutil
import statistics
import sys

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.python._runs import INCLUDES_LOCAL
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import BenchmarkRequest

GREEN = "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"


def _allowed_cpu() -> str:
    """The lowest processor of this process's affinity, else 0 (a platform
    without taskset refuses the request before placing anything)."""
    getaffinity = getattr(os, "sched_getaffinity", None)
    return str(min(getaffinity(0))) if getaffinity else "0"


def _project(tmp_path, text):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text(text, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, root, lb.Suite(str(root / "tests"), ("test_x.py",))


def _tree(root):
    """Every file with its modification time and every directory, except
    bytecode caches, which any pytest run writes."""
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns if p.is_file() else None)
            for p in root.rglob("*") if "__pycache__" not in p.relative_to(root).parts}


def test_measured_repetitions_follow_the_discarded_warmups(tmp_path):
    adapter, root, suite = _project(tmp_path, GREEN)
    before = _tree(root)
    events = []
    result = adapter.benchmark(BenchmarkRequest(
        suite=suite, runs=3, warmup_runs=2, timeout_s=120,
        progress=events.append))
    assert _tree(root) == before
    assert result.outcome is Outcome.COMPLETED and result.reason == ""
    assert len(result.times_ms) == 3 and all(t > 0 for t in result.times_ms)
    assert result.warmup_runs == 2
    assert result.returncode == 0
    assert [(e.kind, e.index, e.total) for e in events] == [
        ("warmup", 1, 2), ("warmup", 2, 2),
        ("measured", 1, 3), ("measured", 2, 3), ("measured", 3, 3),
    ]
    assert all(e.elapsed_ms > 0 for e in events)
    assert result.median_ms == statistics.median(result.times_ms)
    assert result.mean_ms == pytest.approx(statistics.fmean(result.times_ms))
    assert result.min_ms == min(result.times_ms)
    assert result.max_ms == max(result.times_ms)
    assert result.includes == INCLUDES_LOCAL
    assert result.cpus is None
    # Unplaced, the allowed set is this process's, not the processors used.
    getaffinity = getattr(os, "sched_getaffinity", None)
    assert result.allowed_cpus == (
        tuple(sorted(getaffinity(0))) if getaffinity else None)
    assert [f.path for f in result.identity.suite] == ["test_x.py"]


def test_a_repetition_that_does_not_pass_ends_the_benchmark(tmp_path):
    adapter, _root, suite = _project(tmp_path, "def test_a():\n    assert 0\n")
    red = adapter.benchmark(BenchmarkRequest(suite=suite, runs=2,
                                             timeout_s=120))
    assert red.outcome is Outcome.TEST_FAILURES and red.times_ms == ()
    assert red.returncode == 1
    assert (red.median_ms, red.mean_ms, red.min_ms, red.max_ms) == (
        None, None, None, None)
    adapter, _root, suite = _project(tmp_path / "empty", "VALUE = 1\n")
    empty = adapter.benchmark(BenchmarkRequest(suite=suite, runs=1,
                                               timeout_s=120))
    assert empty.outcome is Outcome.NO_TESTS
    adapter, _root, suite = _project(tmp_path / "broken",
                                     "import no_such_module_xyz\n")
    broken = adapter.benchmark(BenchmarkRequest(suite=suite, runs=1,
                                                timeout_s=120))
    assert broken.outcome is Outcome.COLLECTION_FAILED


def test_the_third_of_five_repetitions_reaches_the_timeout(
        tmp_path, monkeypatch):
    """The two completed repetitions are kept, the statistics are absent."""
    counted = ("import os\nimport time\n\n\n"
               "def test_counted():\n"
               "    path = os.environ['BENCHMARK_COUNTER']\n"
               "    count = int(open(path).read()) if os.path.exists(path) "
               "else 0\n"
               "    open(path, 'w').write(str(count + 1))\n"
               "    if count >= 2:\n"
               "        time.sleep(60)\n")
    adapter, _root, suite = _project(tmp_path, counted)
    monkeypatch.setenv("BENCHMARK_COUNTER", str(tmp_path / "counter"))
    result = adapter.benchmark(BenchmarkRequest(
        suite=suite, runs=5, warmup_runs=0, timeout_s=20))
    assert result.outcome is Outcome.TIMED_OUT
    assert result.returncode is None
    assert len(result.times_ms) == 2 and all(t > 0 for t in result.times_ms)
    assert result.median_ms is None and result.max_ms is None


def test_the_processor_set(tmp_path):
    """The repetitions run on the given processor set: the timed test
    asserts its own affinity."""
    cpu = _allowed_cpu()
    placed = ("import os\n\n\ndef test_placed():\n"
              f"    assert os.sched_getaffinity(0) == {{{cpu}}}\n")
    adapter, _root, suite = _project(tmp_path, placed)
    result = adapter.benchmark(BenchmarkRequest(
        suite=suite, runs=1, warmup_runs=0, timeout_s=120, cpus=cpu))
    if shutil.which("taskset"):
        assert result.outcome is Outcome.COMPLETED, result.reason
        assert result.cpus == cpu
        assert result.allowed_cpus == (int(cpu),)
    else:
        assert result.outcome is Outcome.UNSUPPORTED
        assert "taskset" in result.reason and result.cpus == cpu
        assert result.times_ms == ()


def test_invalid_repetitions_name_their_field(tmp_path):
    adapter, _root, suite = _project(tmp_path, GREEN)
    for fields, field in (({"runs": 0}, "runs"),
                          ({"runs": 1, "warmup_runs": -1}, "warmup_runs"),
                          ({"runs": 1, "timeout_s": 0}, "timeout_s"),
                          ({"runs": 1, "cpus": "0-"}, "cpus"),
                          ({"runs": 1, "cpus": "all"}, "cpus"),
                          ({"runs": 1, "progress": 42}, "progress")):
        fields.setdefault("timeout_s", 120)
        with pytest.raises(InvalidRequest) as refused:
            adapter.benchmark(BenchmarkRequest(suite=suite, **fields))
        assert refused.value.field == field


def test_progress_failure_does_not_change_a_measurement(tmp_path):
    adapter, _root, suite = _project(tmp_path, GREEN)

    def broken_progress(event):
        raise RuntimeError("observer failed")

    result = adapter.benchmark(BenchmarkRequest(
        suite=suite, runs=1, warmup_runs=0, timeout_s=120,
        progress=broken_progress))
    assert result.outcome is Outcome.COMPLETED and result.returncode == 0
    assert len(result.times_ms) == 1


def test_the_supervisors_time_of_the_test_process_is_the_measurement(tmp_path, monkeypatch):
    """A repetition's time is the test process's lifetime as the supervisor
    measures it, without its start and stop."""
    import subprocess

    from langadapter import placement

    adapter, _, suite = _project(tmp_path, GREEN)

    def run_timed(command, **options):
        done = subprocess.CompletedProcess(command, 0, "1 passed", "")
        done.native_elapsed_s = 0.25
        return done

    monkeypatch.setattr(placement, "run_timed", run_timed)
    result = adapter.benchmark(BenchmarkRequest(
        suite=suite, runs=2, warmup_runs=1, timeout_s=60))
    assert result.outcome is Outcome.COMPLETED, result.reason
    assert result.times_ms == (250.0, 250.0)
