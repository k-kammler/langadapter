"""Suite profiles of the Python adapter with real pytest in the calling
interpreter: a passing suite's functions and times, and the runs that list
none."""
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import SuiteProfileRequest

TARGET = "def total(n):\n    return sum(range(n))\n\n\nLOADED = total(3)\n"


def _profile(tmp_path, tests, *, timeout_s=120):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "calc.py").write_text(TARGET, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    for name, text in tests.items():
        (tmp_path / "tests" / name).write_text(text, encoding="utf-8")
    adapter = lb.create("python", lb.ProjectContext(
        str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter.tests.profile_suite(SuiteProfileRequest(
        lb.Suite(str(tmp_path / "tests"), tuple(tests)), timeout_s))


def test_a_passing_suites_profile_names_its_functions_with_the_profiler(tmp_path):
    profile = _profile(tmp_path, {"test_calc.py": (
        "from calc import total\n\n\ndef test_total():\n"
        "    assert [total(10) for _ in range(5)] == [45] * 5\n")})
    assert (profile.outcome, profile.profiler, profile.kind) == ("completed", "cProfile", "deterministic")
    assert profile.profiler_version.split()[1] == ".".join(map(str, sys.version_info[:3]))
    found = {(item.path, item.name): item for item in profile.functions}
    total = found[("calc.py", "total")]
    assert (total.start_line, total.calls) == (1, 5)
    assert 0 < total.own_time_s <= total.inclusive_time_s
    assert total.share == total.own_time_s / profile.total_own_time_s
    assert ("tests/test_calc.py", "test_total") in found
    # Imports are not profiled: the module's own code ran at collection.
    assert ("calc.py", "<module>") not in found
    assert all(item.path != "~" for item in profile.functions)


def test_a_root_reached_through_a_link_keeps_relative_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    profile = _profile(link, {"test_calc.py": (
        "from calc import total\n\n\ndef test_total():\n    assert total(4) == 6\n")})
    found = {(item.path, item.name) for item in profile.functions}
    assert ("tests/test_calc.py", "test_total") in found and ("calc.py", "total") in found


@pytest.mark.parametrize("outer_worker", [None, "gw0"])
def test_a_profile_under_pytest_xdist_sums_its_workers(tmp_path, monkeypatch, outer_worker):
    """The sum holds as well for a profile started inside another run's
    pytest-xdist worker, whose variables its pytest inherits."""
    pytest.importorskip("xdist")
    if outer_worker is None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", outer_worker)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n", encoding="utf-8")
    calls = "from calc import total\n\n\ndef test_{0}():\n    assert [total(3) for _ in range(5)] == [3] * 5\n"
    profile = _profile(tmp_path, {"test_one.py": calls.format("one"), "test_two.py": calls.format("two")})
    assert profile.outcome == "completed", profile.reason
    total = next(item for item in profile.functions if (item.path, item.name) == ("calc.py", "total"))
    assert total.calls == 10


def test_failing_tests_and_a_timeout_list_no_functions(tmp_path):
    failing = _profile(tmp_path / "failing", {"test_calc.py": "def test_wrong():\n    assert 1 == 2\n"})
    assert (failing.outcome, failing.functions, failing.total_own_time_s) == ("test_failures", (), None)
    assert failing.reason == "1 test(s) failed or raised an error"
    slow = _profile(tmp_path / "slow", {"test_calc.py": (
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n")}, timeout_s=2)
    assert (slow.outcome, slow.functions) == ("timed_out", ())


def test_a_prepared_copy_profiles_its_own_package_ahead_of_the_originals(tmp_path):
    original = tmp_path / "original"
    (original / "src" / "pkg").mkdir(parents=True)
    (original / "src" / "pkg" / "__init__.py").write_text(
        "def twice(n):\n    return 2 * n\n", encoding="utf-8")
    (original / "tests").mkdir()
    (original / "tests" / "test_pkg.py").write_text(
        "from pkg import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n",
        encoding="utf-8")
    # An installed copy of the package outside the project, on the path.
    installed = tmp_path / "site" / "pkg"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("def twice(n):\n    return n + n\n", encoding="utf-8")
    context = lb.ProjectContext(str(original), interpreter=lb.Interpreter(path=sys.executable),
                                environment=lb.Environment({"PYTHONPATH": str(installed.parent)}))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as scoped:
            profile = lb.create("python", scoped).tests.profile_suite(SuiteProfileRequest(
                lb.Suite(str(Path(scoped.root) / "tests"), ("test_pkg.py",)), 120))
    finally:
        owner.close()
    assert profile.outcome == "completed"
    assert ("src/pkg/__init__.py", "twice") in {(item.path, item.name) for item in profile.functions}


def test_a_containers_profile_paths_are_compared_as_its_posix_paths(monkeypatch):
    """A container names its files in POSIX form, which Windows' Python 3.13
    and later does not take as absolute: a container's names and root are
    compared as the container's paths, never through the host."""
    import ntpath
    import os
    from pathlib import PurePosixPath

    from langadapter.python._runs import _profiled_path

    # Windows' rule since Python 3.13: an absolute path has a drive.
    monkeypatch.setattr(os.path, "isabs", lambda path: bool(ntpath.splitdrive(path)[0]))
    root = PurePosixPath("/work")
    assert _profiled_path("/work/src/a.py", root, True) == "src/a.py"
    assert _profiled_path("/usr/lib/python3.12/json/__init__.py", root, True) == (
        "/usr/lib/python3.12/json/__init__.py")
    assert _profiled_path("<frozen importlib._bootstrap>", root, True) == (
        "<frozen importlib._bootstrap>")


def test_a_partly_written_profile_part_counts_as_missing(tmp_path):
    """A part cut while it was written, as a timeout's kill would leave it,
    is left out, and the run keeps its own outcome."""
    profile = _profile(tmp_path, {"test_calc.py": (
        "import os\nfrom calc import total\n\n\ndef test_total():\n    assert total(4) == 6\n\n\n"
        "def test_a_cut_part():\n"
        "    with open(os.environ['LANGADAPTER_PROFILE'] + '.cut', 'w') as handle:\n"
        "        handle.write('{\"version\": ')\n")})
    assert profile.outcome == "completed", profile.reason
    assert ("calc.py", "total") in {(item.path, item.name) for item in profile.functions}


def test_java_has_no_suite_profile(tmp_path):
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path)))
    (tmp_path / "CalcTest.java").write_text("class CalcTest {}\n", encoding="utf-8")
    profile = adapter.tests.profile_suite(SuiteProfileRequest(lb.Suite(str(tmp_path), ("CalcTest.java",)), 10))
    assert (profile.outcome, profile.functions) == ("unsupported", ())
    assert not adapter.tests.supports("profile_suite")
