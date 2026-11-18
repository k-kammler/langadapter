"""The Cosmic Ray engine on its own with a project context: real cosmic-ray
and pytest in the calling interpreter, no consumer configuration."""
import json
import os
import shlex
import subprocess
import sys
import tempfile

import pytest

import langadapter as lb
from langadapter.execution import LocalExecutor
from langadapter.mutation import FAILED_SESSION, FAILED_WORKERS
from langadapter.python import _cosmic_ray as engine_module, _cr_verify
from langadapter.python._cosmic_ray import CosmicRayEngine
from langadapter.python.cosmic_ray import CosmicRaySettings
from langadapter.python.adapter import PythonAdapter
from langadapter.python.project import _setup_test_env
from langadapter.python._plugins import render
from langadapter.python.project import _PYTEST_CONFIG_COPY

TARGET = ("def add(a, b):\n    return a + b\n\n\n"
          "def is_positive(n):\n    return n > 0\n")
TESTS = ("from arith import add, is_positive\n\n\n"
         "def test_add():\n    assert add(2, 3) == 5\n\n\n"
         "def test_is_positive():\n    assert is_positive(1)\n")
#: One mutant each: ``a - b`` fails test_add; ``n >= 0`` passes both tests.
OPERATORS = ["core/ReplaceBinaryOperator_Add_Sub",
             "core/ReplaceComparisonOperator_Gt_GtE"]


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        completed = subprocess.run([executable, "--help"],
                                   capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


needs_cosmic_ray = pytest.mark.skipif(
    not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")


def _adapter(root):
    context = lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig("isolated"))
    return PythonAdapter(context)


def _engine(adapter, config):
    """The Cosmic Ray engine of ``adapter``'s project."""
    return CosmicRayEngine({**CosmicRaySettings()._engine_config("local"), **config}, adapter=adapter)


def _session_path(engine, root):
    """The session database of ``engine``'s open session for ``root``, where
    the session's capture reads it."""
    return engine._container_session_path() or engine._resolve_paths()[0]


def _matrix(engine, root, target):
    """The kill matrix of the open session's last run, read as the session's
    capture reads it and saved as JSON."""
    run = engine._read_mutation_run(_session_path(engine, root), target)
    return json.loads(json.dumps(run.to_kill_matrix()))


def _project(root, test_dir):
    root.mkdir(parents=True)
    test_dir.mkdir(parents=True, exist_ok=True)
    (root / "arith.py").write_text(TARGET, encoding="utf-8")
    (test_dir / "test_arith.py").write_text(TESTS, encoding="utf-8")


#: A suite conftest that makes pytest-benchmark's default storage in the
#: working directory when pytest configures, as that plugin does.
STORAGE = ("import os\n\n\n"
           "def pytest_configure(config):\n"
           "    os.makedirs('.benchmarks', exist_ok=True)\n")


def test_a_session_removes_the_empty_benchmark_storage_it_created(tmp_path):
    """The session's runs start pytest in the project root, where
    pytest-benchmark makes .benchmarks. The session notes the storage when
    it opens and removes it when it closes, when the session made it and it
    is still empty. One that was there before, or holds a file, stays."""
    root = tmp_path / "proj"
    _project(root, root / "tests")
    engine = _engine(_adapter(root), {})
    with engine.session(project_root=root, workers=1):
        (root / ".benchmarks").mkdir()
    assert not (root / ".benchmarks").exists()
    with engine.session(project_root=root, workers=1):
        (root / ".benchmarks").mkdir()
        (root / ".benchmarks" / "saved.json").write_text("{}", encoding="utf-8")
    assert (root / ".benchmarks" / "saved.json").is_file()
    (root / ".benchmarks" / "saved.json").unlink()
    with engine.session(project_root=root, workers=1):
        pass
    assert (root / ".benchmarks").is_dir()


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_session_whose_runs_make_the_benchmark_storage_leaves_none(tmp_path):
    """A plugin that makes the storage whenever pytest configures, here the
    suite's conftest, leaves none in the project after the session."""
    root = tmp_path / "proj"
    _project(root, root / "tests")
    (root / "tests" / "conftest.py").write_text(STORAGE, encoding="utf-8")
    engine = _engine(_adapter(root), {})
    with engine.session(project_root=root, workers=1) as session:
        session.run(target=root / "arith.py", test_dir=root / "tests",
                    per_mutant_timeout=60, wall_clock_timeout=600, operators=OPERATORS)
        assert (root / ".benchmarks").is_dir()
    assert not (root / ".benchmarks").exists()


def test_the_origin_check_keeps_each_path_one_argument(tmp_path):
    """The check before the first mutant adds its report to the per-mutant
    command: a project and a scratch directory whose names hold a space and an
    apostrophe pass it."""
    root = tmp_path / "my proj's"
    _project(root, root / "tests")
    engine = _engine(_adapter(root), {})
    command = shlex.join([
        sys.executable.replace(os.sep, "/"), "-m", "pytest",
        (root / "tests" / "test_arith.py").as_posix(), "-q", "-p", "no:cacheprovider",
        f"--rootdir={(root / 'tests').as_posix()}"])
    engine._origin_check(command, target=str(root / "arith.py"), root=str(root),
                         cwd=str(root), env=dict(os.environ, PYTHONPATH=str(root)),
                         scratch=tmp_path / "scratch dir's", timeout=120,
                         files=("test_arith.py",))
    assert (tmp_path / "scratch dir's" / "origin.xml").is_file()


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_run_in_the_context_interpreter_classifies_each_mutant(tmp_path):
    root = tmp_path / "proj"
    _project(root, root / "tests")
    adapter = _adapter(root)
    engine = _engine(adapter, {})
    assert isinstance(engine, CosmicRayEngine) and engine.adapter is adapter
    with engine.session(project_root=root, workers=1) as session:
        assert isinstance(session, engine_module.CosmicRayEngineSession)
        truncated = session.run(
            target=root / "arith.py", test_dir=root / "tests",
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=OPERATORS)
        # The matrix comes from the session's files: capture it while open.
        matrix = _matrix(engine, root, root / "arith.py")
        session_path = _session_path(engine, root)
    assert truncated is False
    summary = matrix["summary"]
    assert (summary["detected"], summary["survived"], summary["untested"]) == (1, 1, 0)
    by_operator = {m["operator"]: m for m in matrix["mutants"]}
    assert set(by_operator) == set(OPERATORS)
    killed = by_operator["core/ReplaceBinaryOperator_Add_Sub"]
    assert killed["status"] == "killed"
    assert killed["killer"].endswith("test_add")
    assert by_operator["core/ReplaceComparisonOperator_Gt_GtE"]["status"] == (
        "survived")
    assert matrix["catalogue_provenance"]["requested_catalogue"] == "filtered"
    assert "incomplete" not in matrix
    # The session directory goes with the session.
    assert not session_path.exists()


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_session_leaves_no_configuration_copy_in_the_project(tmp_path):
    """The copy of the project's pytest configuration that runs put beside
    the suite goes when the session closes. A project file of that name
    stays."""
    root = tmp_path / "proj"
    _project(root, root / "tests")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nmarkers = ['slow: slow tests']\n",
        encoding="utf-8")
    # The project's own pytest configuration applies (no isolated framework).
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    engine = _engine(adapter, {})
    copy = root / "tests" / f"{_PYTEST_CONFIG_COPY}.toml"
    other = root / "tests" / f"{_PYTEST_CONFIG_COPY}.ini"
    other.write_text("[pytest]\n", encoding="utf-8")
    with engine.session(project_root=root, workers=1) as session:
        session.run(
            target=root / "arith.py", test_dir=root / "tests",
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=OPERATORS)
        assert copy.is_file()
        summary = _matrix(engine, root, root / "arith.py")["summary"]
    assert (summary["detected"], summary["survived"]) == (1, 1)
    assert not copy.exists()
    assert other.read_text(encoding="utf-8") == "[pytest]\n"


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_http_workers_import_their_clone_in_a_sibling_layout(tmp_path):
    """Tests outside the project root stay in the live directory and reach
    each worker's clone only through the source-root variable that the
    engine sets and the prepared conftest reads, both with the variable prefix."""
    work = tmp_path / "work"
    root, test_dir = work / "proj", work / "tests"
    _project(root, test_dir)
    adapter = _adapter(root)
    _setup_test_env(adapter, str(test_dir), str(root))
    conftest = (test_dir / "conftest.py").read_text(encoding="utf-8")
    assert "LANGADAPTER_MUTATION_SOURCE_ROOT" in conftest
    engine = _engine(adapter, {"cosmic_ray_distributor": "http"})
    with engine.session(project_root=root, workers=2) as session:
        assert session.use_http and len(session.worker_urls) == 2
        session.run(
            target=root / "arith.py", test_dir=test_dir,
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=OPERATORS)
        summary = _matrix(engine, root, root / "arith.py")["summary"]
    assert (summary["detected"], summary["survived"], summary["untested"]) == (1, 1, 0)


@pytest.mark.mutation_tool
@needs_cosmic_ray
@pytest.mark.parametrize("workers", [1, 2])
def test_a_temporary_directory_beside_the_suite_hides_nothing_the_run_needs(
        tmp_path, monkeypatch, workers):
    """With a temporary directory beside the suite, each mutant's run has
    a directory of its own inside it. The suite and project beside that
    directory stay visible, so both mutants are measured. One worker runs
    in place, two in clones."""
    scratch = tmp_path / "scratch" / "tmp"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)
    work = tmp_path / "work"
    root, test_dir = work / "proj", work / "tests"
    _project(root, test_dir)
    adapter = _adapter(root)
    _setup_test_env(adapter, str(test_dir), str(root))
    engine = _engine(adapter, 
        {"cosmic_ray_distributor": "http"} if workers > 1 else {})
    with engine.session(project_root=root, workers=workers) as session:
        session.run(
            target=root / "arith.py", test_dir=test_dir,
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=OPERATORS)
        summary = _matrix(engine, root, root / "arith.py")["summary"]
    assert (summary["detected"], summary["survived"], summary["non_viable"],
            summary["untested"]) == (1, 1, 0, 0), summary


@pytest.mark.mutation_tool
@needs_cosmic_ray
@pytest.mark.parametrize("workers", [1, 2])
def test_a_suite_rooted_at_the_project_measures_alike_in_place_and_in_clones(
        tmp_path, workers):
    """Copying a suite rooted at the project root into each worker's clone
    must not remove the clone, the worker's working directory. One worker
    in place and two in clones measure the same verdicts."""
    root = tmp_path / "proj"
    _project(root, root / "tests")
    engine = _engine(_adapter(root), 
        {"cosmic_ray_distributor": "http"} if workers > 1 else {})
    with engine.session(project_root=root, workers=workers) as session:
        assert session.use_http == (workers > 1)
        session.run(
            target=root / "arith.py", test_dir=root,
            test_files=["tests/test_arith.py"], per_mutant_timeout=60,
            wall_clock_timeout=600, operators=OPERATORS)
        summary = _matrix(engine, root, root / "arith.py")["summary"]
    assert (summary["detected"], summary["survived"], summary["non_viable"],
            summary["untested"]) == (1, 1, 0, 0), summary


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_run_whose_workers_all_fail_is_no_measurement(tmp_path, monkeypatch):
    """Cosmic Ray reports a worker exception as an incompetent mutant, which
    measured nothing. A run in which every worker failed is an infrastructure failure,
    not a score. Here the module is missing from every clone once the check
    before the first mutant passed there."""
    from langadapter.mutation import Catalogue, MutationRequest

    root, clones = tmp_path / "proj", tmp_path / "clones"
    _project(root, root / "tests")

    check = CosmicRayEngine._origin_check

    def then_remove_the_module(self, *args, root, **options):
        check(self, *args, root=root, **options)
        # Only this session's clones: the shared default base also holds
        # other runs' clones.
        for clone in clones.glob("cr_worker_*"):
            if clone.is_dir():
                (clone / "arith.py").unlink()

    monkeypatch.setattr(CosmicRayEngine, "_origin_check", then_remove_the_module)
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    with adapter.open_mutation_session(
            workers=2, distributor="http",
            settings=CosmicRaySettings(clone_dir=str(clones))) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
            catalogue=Catalogue(operators=tuple(OPERATORS))))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE, (run.outcome,
                                                              run.reason)
    assert "for every one of the 2 mutants" in run.reason
    assert run.score() is None and run.failure == FAILED_WORKERS


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_run_without_any_test_outcome_is_no_measurement(tmp_path, monkeypatch):
    """A mutant without a test outcome is untested and the rest of the run
    still scores. A run in which no mutant has one is no measurement. Here
    every clone's module lost the mutated code (NO_TEST) once the check
    before the first mutant passed there."""
    from langadapter.mutation import Catalogue, MutationRequest

    root, clones = tmp_path / "proj", tmp_path / "clones"
    _project(root, root / "tests")

    check = CosmicRayEngine._origin_check

    def then_empty_the_module(self, *args, root, **options):
        check(self, *args, root=root, **options)
        # Only this session's clones, as above.
        for clone in clones.glob("cr_worker_*"):
            if clone.is_dir():
                (clone / "arith.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(CosmicRayEngine, "_origin_check", then_empty_the_module)
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    with adapter.open_mutation_session(
            workers=2, distributor="http",
            settings=CosmicRaySettings(clone_dir=str(clones))) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
            catalogue=Catalogue(operators=tuple(OPERATORS))))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE, (run.outcome, run.reason)
    assert run.reason.startswith("none of the 2 mutants has a test outcome"), run.reason
    assert run.score() is None and run.failure == FAILED_WORKERS


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_local_run_gives_its_progress_callback_to_the_session_poller(
        tmp_path, monkeypatch):
    from langadapter.mutation import Catalogue, MutationRequest

    root = tmp_path / "proj"
    _project(root, root / "tests")
    polled, events = [], []
    poll = _cr_verify._poll_session_progress

    def recorded(**kwargs):
        polled.append(kwargs["progress"])
        poll(**kwargs)

    monkeypatch.setattr(_cr_verify, "_poll_session_progress", recorded)
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    with adapter.open_mutation_session(workers=1) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
            catalogue=Catalogue(operators=tuple(OPERATORS)), progress=events.append))
    assert polled == [events.append]
    assert run.outcome is lb.Outcome.COMPLETED and run.score() == 50.0
    # A short run may end before the first one-second tick.
    assert all(e.kind == "mutants" and e.total in (None, 2) and 0 <= e.finished <= 2
               for e in events), events


#: ``n - 1`` made ``n + 1``, in the module's last lines, hangs the first
#: test. No test reaches ``unused``. The tests leave no state behind: no run
#: restores the module between them.
HANG_TARGET = ("FLAG = 0\n\n\n"
               "def flag():\n    return FLAG\n\n\n"
               "def unused(x):\n    return x - 2\n\n\n"
               "def countdown(n):\n    while n > 0:\n        n = n - 1\n"
               "    return n\n")
HANG_TESTS = ("import arith\nfrom arith import countdown\n\n\n"
              "def test_a_counts_down():\n    assert countdown(3) == 0\n\n\n"
              "def test_b_reads_the_flag():\n    assert arith.flag() == 0\n")


@pytest.mark.mutation_tool
@needs_cosmic_ray
@pytest.mark.parametrize("temporary, workers", [("inherited", 1), ("set", 2)])
def test_a_local_run_journals_and_names_the_hanging_test(
        tmp_path, monkeypatch, temporary, workers):
    """A local run writes the per-test journal, so the mutant that makes
    countdown loop keeps its timeout verdict with the hung test as killer,
    and the mutant no test reaches survives. The suite carries no
    instrument, so the run journals through the adapter's plugin, as a
    session's run of such a suite does. The module has CRLF line ends, and the
    looping mutant lies in its last lines, where the diff names the empty
    element after the final newline. With a temporary directory set, each
    mutant's run has a directory of its own inside it, and the journals in the
    session directory below it stay visible. Two workers run in clones, one
    in place."""
    if temporary == "set":
        (tmp_path / "tmp").mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
        monkeypatch.setattr(tempfile, "tempdir", None)
    root = tmp_path / "proj"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "arith.py").write_bytes(
        HANG_TARGET.replace("\n", "\r\n").encode("utf-8"))
    (tests / "test_arith.py").write_text(HANG_TESTS, encoding="utf-8")
    adapter = _adapter(root)
    _setup_test_env(adapter, str(tests), str(root))
    engine = _engine(adapter, {"_journal_plugin": True})
    with engine.session(project_root=root, workers=workers) as session:
        session.run(
            target=root / "arith.py", test_dir=tests,
            per_mutant_timeout=10, wall_clock_timeout=600,
            operators=["core/ReplaceBinaryOperator_Sub_Add"])
        matrix = _matrix(engine, root, root / "arith.py")
    by_line = {mutant["line"]: mutant for mutant in matrix["mutants"]}
    assert sorted(by_line) == [9, 14]
    hanging, unreached = by_line[14], by_line[9]
    assert hanging["status"] == "timeout", hanging
    assert hanging["timeout_evidence"] == "hang"
    assert hanging["killer"].endswith("test_a_counts_down")
    assert unreached["status"] == "survived", unreached


#: ``n < 0`` made ``n > 0`` ends the test process in the middle of
#: test_check; ``n * 2`` made ``n + 2`` fails test_double.
CRASH_TARGET = ("import os\n\n\n"
                "def check(n):\n    if n < 0:\n        os._exit(3)\n"
                "    return n\n\n\n"
                "def double(n):\n    return n * 2\n")
CRASH_TESTS = ("from guard import check, double\n\n\n"
               "def test_check():\n    assert check(1) == 1\n\n\n"
               "def test_double():\n    assert double(3) == 6\n")


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_local_run_observes_the_test_a_mutant_ended(tmp_path):
    """With the journal, a mutant that ends the test process is an
    unresolved kill without a killer. The test running at the end is
    recorded as an observation, not as the killer. The suite carries no
    instrument, so the journal comes from the adapter's plugin."""
    root = tmp_path / "proj"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "guard.py").write_text(CRASH_TARGET, encoding="utf-8")
    (tests / "test_guard.py").write_text(CRASH_TESTS, encoding="utf-8")
    adapter = _adapter(root)
    _setup_test_env(adapter, str(tests), str(root))
    engine = _engine(adapter, {"_journal_plugin": True})
    with engine.session(project_root=root, workers=1) as session:
        session.run(
            target=root / "guard.py", test_dir=tests,
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=["core/ReplaceComparisonOperator_Lt_Gt",
                       "core/ReplaceBinaryOperator_Mul_Add"])
        matrix = _matrix(engine, root, root / "guard.py")
    by_line = {mutant["line"]: mutant for mutant in matrix["mutants"]}
    assert sorted(by_line) == [5, 11]
    ended, failed = by_line[5], by_line[11]
    assert ended["status"] == "killed", ended
    assert not (ended.get("killers") or ended.get("killer"))
    assert ended["unresolved"] is True
    assert ended["active_at_end"].endswith("test_check")
    assert failed["status"] == "killed", failed
    assert failed["killer"].endswith("test_double")
    assert "active_at_end" not in failed


def test_the_names_a_run_writes_carry_the_variable_prefix(monkeypatch, tmp_path):
    wrapper = render(engine_module._PER_RUN_WRAPPER_SH)
    assert "$LANGADAPTER_FDLIMIT_DEBUG" in wrapper and "@PREFIX@" not in wrapper
    assert "$LANGADAPTER_FDLEAK_WATCH" in wrapper
    prefix = engine_module._write_per_run_wrapper(tmp_path, "python")
    written = next(tmp_path.iterdir()).read_text(encoding="utf-8")
    if os.name == "nt":
        assert prefix[0] == "python" and "@PREFIX@" not in written
    else:
        assert prefix[0] == "sh" and written == wrapper
    monkeypatch.delenv("LANGADAPTER_MUTATION_DIAG_TB", raising=False)
    assert engine_module._mutation_diag_tb() == "no"
    monkeypatch.setenv("LANGADAPTER_MUTATION_DIAG_TB", "short")
    assert engine_module._mutation_diag_tb() == "short"


def test_an_engine_names_the_project_it_cannot_reach():
    with pytest.raises(RuntimeError, match="no Python adapter"):
        CosmicRayEngine({})._project_adapter()


def test_the_engine_rebases_session_paths_onto_the_contexts_work_dir(
        tmp_path):
    """The session specification's paths reach the container's work directory
    the executor uses: the context's explicit one, else the default."""
    def engine(**execution):
        context = lb.ProjectContext(
            root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
            execution=lb.ContainerExecution(container="c", **execution))
        return CosmicRayEngine(CosmicRaySettings()._engine_config("local"), adapter=PythonAdapter(context))

    zone = str(tmp_path)
    default = "/workzone" if os.name == "nt" else tmp_path.as_posix()
    assert engine(work_dir=zone)._project_adapter()._container_work_dir() == default
    assert engine(work_dir=zone, container_work_dir="/mnt/wz"
                  )._project_adapter()._container_work_dir() == "/mnt/wz"


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_concurrent_workers_kill_no_mutant_no_test_reaches(tmp_path):
    """Each HTTP worker run starts in its own scratch directory, so with
    several workers at once a mutant no test reaches survives. In a shared
    directory concurrent runs fail before testing, which counts as kills."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("".join(
        f"def f{i}(a, b):\n    return a + b * {i}\n\n" for i in range(6)),
        encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "def test_nothing():\n    assert True\n", encoding="utf-8")
    engine = _engine(_adapter(root), {"cosmic_ray_distributor": "http"})
    with engine.session(project_root=root, workers=3) as session:
        assert len(session.worker_urls) == 3
        session.run(
            target=root / "calc.py", test_dir=root / "tests",
            per_mutant_timeout=60, wall_clock_timeout=600,
            operators=["core/ReplaceBinaryOperator_Add_Sub",
                       "core/ReplaceBinaryOperator_Mul_Div"])
        summary = _matrix(engine, root, root / "calc.py")["summary"]
    assert summary["total"] == 12 and summary["survived"] == 12, summary


def _interpreter_of_another_version():
    """An installed Python 3 of another major.minor than the caller's, or
    None."""
    import shutil

    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    if os.name == "nt":
        try:
            listing = subprocess.run(["py", "-0p"], capture_output=True,
                                     text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            listing = ""
        candidates = [line.split()[-1] for line in listing.splitlines()
                      if line.strip()]
    else:
        candidates = [shutil.which(name) for name in (
            "python3.10", "python3.11", "python3.13", "python3.9",
            "python3")]
    for path in filter(None, candidates):
        try:
            version = subprocess.run(
                [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if version.startswith("3.") and version != here:
            return path
    return None


def test_the_catalogue_cache_is_keyed_by_the_project_interpreter(
        tmp_path, monkeypatch):
    other = _interpreter_of_another_version()
    if other is None:
        pytest.skip("needs a second Python 3 of another minor version")
    target = tmp_path / "arith.py"
    target.write_text(TARGET, encoding="utf-8")

    def cache_path(python):
        engine = CosmicRayEngine(
            {**CosmicRaySettings()._engine_config("local"), "cosmic_ray_catalogue_cache_dir": str(tmp_path / "cache")},
            adapter=PythonAdapter(lb.ProjectContext(
                root=str(tmp_path), interpreter=lb.Interpreter(path=python))))
        # One Cosmic Ray version for both: only the probed interpreter
        # version differs in the key.
        probed = engine._cosmic_ray_metadata()
        monkeypatch.setattr(engine, "_cosmic_ray_metadata",
                            lambda: ("8.4.1", None, probed[2]))
        return engine._resolve_cache_path(target, OPERATORS)

    here, there = cache_path(sys.executable), cache_path(other)
    assert here is not None and there is not None and here != there
    assert cache_path(sys.executable) == here
    # An interpreter that cannot be asked leaves the cache off.
    not_python = tmp_path / "not_python.txt"
    not_python.write_text("text\n", encoding="utf-8")
    assert cache_path(str(not_python)) is None


def test_the_metadata_probe_reports_the_interpreter_version():
    here = "py%d.%d" % sys.version_info[:2]
    assert engine_module._project_cosmic_ray_metadata(
        LocalExecutor(), sys.executable, flags=())[2] == here

    class Answering:
        def __init__(self, interpreter):
            self.interpreter = interpreter

        def run(self, argv, timeout):
            payload = {"version": "8.4.1", "operators": ["core/A"],
                       "python": self.interpreter}
            return subprocess.CompletedProcess(
                argv, 0, engine_module._PROJECT_METADATA_PREFIX
                + json.dumps(payload) + "\n", "")

    for reported, read in (("py3.12", "py3.12"), ("3.12", None), (None, None)):
        assert engine_module._project_cosmic_ray_metadata(
            Answering(reported), "python", flags=()) == ("8.4.1", ["core/A"], read)


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_an_interrupt_while_the_http_workers_stop_reaches_the_caller(tmp_path, monkeypatch):
    """A stop while the session stops its HTTP workers reaches the caller as
    KeyboardInterrupt, not as a cleanup failure. The session keeps what it
    did not release and refuses runs until a retried close finishes."""
    from langadapter import _process_tree
    from langadapter.mutation import Catalogue, MutationRequest

    root = tmp_path / "proj"
    _project(root, root / "tests")
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    session = adapter.open_mutation_session(workers=2, distributor="http")
    native = session._session
    workers, clones = list(native._worker_procs), list(native._worker_clones)
    assert session.workers_started == 2 and len(workers) == 2 and len(clones) == 2
    stop_all = _process_tree.stop_all

    def stopped_by_the_user(procs, **options):
        monkeypatch.setattr(_process_tree, "stop_all", stop_all)
        raise KeyboardInterrupt

    monkeypatch.setattr(_process_tree, "stop_all", stopped_by_the_user)
    with pytest.raises(KeyboardInterrupt) as caught:
        session.close()
    assert caught.value.cleanup_handle is session
    assert session._session is native and any(clone.exists() for clone in clones)
    run = session.run(MutationRequest(
        target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
        catalogue=Catalogue(operators=tuple(OPERATORS))))
    assert run.score() is None and "cleanup must be retried" in run.reason
    session.close()
    assert session._session is None
    assert all(worker.poll() is not None for worker in workers)
    assert not any(clone.exists() for clone in clones)


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_an_explicit_http_session_of_one_worker_runs_its_worker(tmp_path):
    """An explicit http distributor with one worker starts one HTTP worker
    and measures with it. The default with one worker stays local."""
    from langadapter.mutation import Catalogue, MutationRequest

    root = tmp_path / "proj"
    _project(root, root / "tests")
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    request = MutationRequest(
        target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
        catalogue=Catalogue(operators=tuple(OPERATORS)))
    with adapter.open_mutation_session(workers=1, distributor="http") as session:
        assert session.distributor == "http" and session.workers_started == 1
        assert session._session.use_http and len(session._session.worker_urls) == 1
        run = session.run(request)
        assert session._nested_session is None
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert run.score() == 50.0
    with adapter.open_mutation_session(workers=1) as session:
        assert session.distributor == "local" and not session._session.use_http


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_launch_that_left_nothing_running_fails_only_its_run(tmp_path, monkeypatch):
    """A cosmic-ray exec launch whose first Popen raised started nothing to
    dispose of: that run gives no result and the session measures the next.
    The Popen is the target's on Windows and other POSIX hosts, the
    supervisor helper's on Linux."""
    import errno
    import subprocess as sp

    from langadapter import _process_tree
    from langadapter.mutation import Catalogue, MutationRequest

    root = tmp_path / "proj"
    _project(root, root / "tests")
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    request = MutationRequest(
        target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
        catalogue=Catalogue(operators=tuple(OPERATORS)))
    state = {"armed": False, "failed": 0}
    start, popen = _process_tree.start, sp.Popen

    def exec_start(args, **options):
        state["armed"] = "exec" in args and any("cosmic" in str(a) for a in args)
        try:
            return start(args, **options)
        finally:
            state["armed"] = False

    def refused(*args, **options):
        if state["armed"] and not state["failed"]:
            state["failed"] += 1
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable (test)")
        return popen(*args, **options)

    monkeypatch.setattr(_process_tree, "start", exec_start)
    monkeypatch.setattr(sp, "Popen", refused)
    with adapter.open_mutation_session(workers=1) as session:
        failed = session.run(request)
        assert state["failed"] == 1
        # The run failed alone and measured nothing: no score, not complete,
        # and the reason names the exec that did not start.
        assert failed.score() is None and not failed.complete
        assert failed.failure == FAILED_SESSION
        assert failed.reason.startswith("the mutation tool's exec did not start (")
        assert "Resource temporarily unavailable (test)" in failed.reason
        assert session._session._target_commands == []
        measured = session.run(request)
    assert measured.outcome is lb.Outcome.COMPLETED, measured.reason
    assert measured.score() == 50.0


@pytest.mark.mutation_tool
@needs_cosmic_ray
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("stage", ["origin check", "exec"])
@pytest.mark.parametrize("via", ["session", "scope"])
def test_a_cancel_ends_a_measurement_in_flight(tmp_path, workers, stage, via, monkeypatch):
    """A cancel from the main thread, as on Ctrl+C, ends a run in another
    thread within seconds with MeasurementCancelled. The session closes, its
    workers stop and its directory goes, with the killed run's temporary
    directory and marker. A CancelScope around the run cancels it alike,
    and the session's cleanup runs inside the cancelled scope."""
    import time as clock
    from concurrent.futures import ThreadPoolExecutor

    from langadapter.mutation import Catalogue, MutationRequest
    from langadapter.mutation import MeasurementCancelled

    root = tmp_path / "proj"
    _project(root, root / "tests")
    (root / "tests" / "test_arith.py").write_text(
        "import time\n" + TESTS.replace("    assert add", "    time.sleep(20)\n    assert add"),
        encoding="utf-8")
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    request = MutationRequest(
        target="arith.py", suite=lb.Suite(str(root), ("tests/test_arith.py",)),
        catalogue=Catalogue(operators=tuple(OPERATORS)), wall_clock_limit_s=600)
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    session = adapter.open_mutation_session(workers=workers)
    native = session._session
    procs = list(native._worker_procs)
    scope = lb.CancelScope()

    def measure():
        if via == "session":
            return session.run(request)
        with scope:
            return session.run(request)

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(measure)
        deadline = clock.monotonic() + 180

        def reached():
            live = list(session._cancellation._live) if session._cancellation else []
            if stage == "exec":
                # Each run makes its temporary directory in TMPDIR.
                return any("exec" in [str(a) for a in getattr(p, "args", ())] for p in live) and (
                    any(scratch.glob("cr_runtmp_*")))
            return bool(live)

        while not reached():
            assert clock.monotonic() < deadline and not future.done()
            clock.sleep(0.1)
        clock.sleep(1)
        session_dir = native._session_dir or getattr(native.engine, "_default_session_dir", None)
        started = clock.monotonic()
        (session.cancel if via == "session" else scope.cancel)()
        with pytest.raises(MeasurementCancelled):
            future.result(timeout=120)
    assert clock.monotonic() - started < 30
    assert session._session is None
    assert all(proc.poll() is not None for proc in procs)
    assert session_dir is None or not session_dir.exists()
    assert sorted(p.name for p in scratch.glob("cr_runtmp_*")) == []
    session.close()


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_mutation_run_of_a_project_named_relative_to_another_dir(tmp_path, monkeypatch):
    """A relative suite root, as the examples pass it, is resolved before
    the origin check, so the check finds the project's pytest configuration
    and the run records where the tests import the tested module from."""
    from langadapter.mutation import Catalogue, MutationRequest

    root = tmp_path / "proj"
    _project(root, root / "tests")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = \"-q\"\n",
                                         encoding="utf-8")
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.chdir(tmp_path)
    adapter = lb.create("python", lb.ProjectContext(
        "proj", lb.Interpreter(path=sys.executable)))
    request = MutationRequest(
        target="arith.py", suite=lb.Suite("proj", ("tests/test_arith.py",)),
        catalogue=Catalogue(operators=tuple(OPERATORS)))
    with adapter.open_mutation_session(workers=1) as session:
        run = session.run(request)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert run.score() == 50.0


@pytest.mark.mutation_tool
@needs_cosmic_ray
def test_a_catalogue_cache_entry_that_is_no_session_is_dropped_and_rebuilt(tmp_path):
    """A cache entry that copies but is no session database (an interrupted
    write) is removed with what it left at the session's path, the run
    initializes afresh, measures, and stores a usable entry again."""
    root = tmp_path / "proj"
    _project(root, root / "tests")
    logs = []
    engine = _engine(_adapter(root), {
        "cosmic_ray_catalogue_cache_dir": str(tmp_path / "cache"),
        "log_fn": lambda message, level=0: logs.append(str(message))})
    run = dict(target=root / "arith.py", test_dir=root / "tests",
               per_mutant_timeout=60, wall_clock_timeout=600, operators=OPERATORS)
    with engine.session(project_root=root, workers=1) as session:
        session.run(**run)
    cache = engine._resolve_cache_path(root / "arith.py", OPERATORS, module_path="arith.py")
    assert cache is not None and cache.is_file()
    cache.write_bytes(b"not a session database")
    logs.clear()
    with engine.session(project_root=root, workers=1) as session:
        session.run(**run)
        summary = _matrix(engine, root, root / "arith.py")["summary"]
    assert any("catalogue cache restore failed" in line for line in logs), logs[-10:]
    assert summary["detected"] + summary["survived"] == 2, summary
    engine_module._validate_session_db(cache)
