"""Every Python test run of a suite is the one test run: the normal run, the
coverage tool's run, the per-test evidence and Cosmic Ray's runs give a test
the same environment, so a test that passes the normal run passes under the
tools."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.outcomes import Outcome
from langadapter.python import _cosmic_ray as engine_module
from langadapter.python._run_definition import RUN_VARIABLES, THREAD_VARIABLES
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest

#: The determinism instrument as a suite delivered with it carries it.
CARRIED_INSTRUMENT = Path(__file__).with_name("carried_instrument.txt").read_text(
    encoding="utf-8")

TARGET = "def add(a, b):\n    return a + b\n"
#: The variables of a pytest-xdist worker the caller runs in.
OUTER_WORKER = {"PYTEST_XDIST_WORKER": "gw0", "PYTEST_XDIST_WORKER_COUNT": "4",
                "PYTEST_XDIST_TESTRUNUID": "outer"}
ENVIRONMENT = (
    "import os\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_the_run_environment():\n"
    "    assert os.environ.get('PYTHONHASHSEED') == '0'\n"
    f"    for name in {THREAD_VARIABLES!r}:\n"
    "        assert os.environ.get(name) == '1', name\n"
    f"    assert [name for name in {tuple(OUTER_WORKER)!r} if name in os.environ] == []\n")


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _project(tmp_path, tests):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "arith.py").write_text(TARGET, encoding="utf-8")
    (root / "tests" / "test_arith.py").write_text(tests, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, lb.Suite(str(root / "tests"), ("test_arith.py",))


def test_the_run_variables_are_one_set():
    assert RUN_VARIABLES == {"PYTHONHASHSEED": "0", **{name: "1" for name in THREAD_VARIABLES}}


@pytest.fixture
def outer_worker(monkeypatch):
    """The caller runs in a pytest-xdist worker, whose variables its runs
    would inherit."""
    for name, value in OUTER_WORKER.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("selected, kept", [({}, {}), ({"PYTEST_XDIST_WORKER": "gw7"},
                                                       {"PYTEST_XDIST_WORKER": "gw7"})])
def test_a_callers_xdist_worker_variables_reach_a_run_only_as_selected(
        tmp_path, monkeypatch, outer_worker, selected, kept):
    """A run started inside a pytest-xdist worker of the caller is not that
    worker: the normal run and the mutants' runs leave out the worker's
    variables they would inherit, and keep those the context sets."""
    monkeypatch.setenv("AMBIENT", "native")
    adapter = PythonAdapter(lb.ProjectContext(
        str(tmp_path), interpreter=lb.Interpreter(path=sys.executable),
        environment=lb.Environment(selected)))
    for value in (adapter._build_env(str(tmp_path)),
                  engine_module.CosmicRayEngine({}, adapter=adapter)._build_env(tmp_path)):
        assert value["AMBIENT"] == "native"
        assert {name: value[name] for name in OUTER_WORKER if name in value} == kept


def test_the_normal_coverage_and_evidence_runs_give_the_run_environment(tmp_path, outer_worker):
    adapter, suite = _project(tmp_path, ENVIRONMENT)
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert normal.outcome is Outcome.COMPLETED, normal.output
    covered = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120, coverage_target="arith.py"))
    assert covered.outcome is Outcome.COMPLETED, covered.output
    evidence = adapter.tests.per_test_evidence(PerTestEvidenceRequest(
        suite, target="arith.py", timeout_s=120))
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_cosmic_ray_runs_give_the_run_environment(tmp_path, options, outer_worker):
    """The origin check and each mutant run the test command Cosmic Ray
    starts: a test that holds the run environment passes there too."""
    adapter, suite = _project(tmp_path, ENVIRONMENT)
    with adapter.open_mutation_session(**options) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


# Increments B and C: the run's root and its files, its own temporary
# directory, and no user namespace.

ROOT_AND_FILES = (
    "import os\nimport tempfile\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_reads_a_project_file_from_the_root():\n"
    "    with open('data.txt', encoding='utf-8') as handle:\n"
    "        assert handle.read() == 'data'\n\n\n"
    "def test_finds_no_file_an_earlier_run_left():\n"
    "    assert not os.path.exists('left.txt')\n"
    "    with open('left.txt', 'w', encoding='utf-8') as handle:\n"
    "        handle.write('left')\n\n\n"
    "def test_starts_with_an_empty_temporary_dir():\n"
    "    directory = tempfile.gettempdir()\n"
    "    assert not os.path.exists(os.path.join(directory, 'langadapter-left.txt'))\n"
    "    with open(os.path.join(directory, 'langadapter-left.txt'), 'w', encoding='utf-8') as handle:\n"
    "        handle.write('left')\n")


def _prepared(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / "arith.py").write_text(TARGET, encoding="utf-8")
    (source / "data.txt").write_text("data", encoding="utf-8")
    (source / "tests" / "test_arith.py").write_text(ROOT_AND_FILES, encoding="utf-8")
    return lb.prepare_project(lb.ProjectContext(str(source), lb.Interpreter(path=sys.executable)),
                              parent=str(tmp_path), receipt_parent=str(tmp_path))


def test_two_normal_runs_start_in_the_root_without_what_the_first_left(tmp_path):
    prepared = _prepared(tmp_path)
    with prepared.use() as context:
        adapter = lb.create("python", context)
        suite = lb.Suite(str(Path(context.root) / "tests"), ("test_arith.py",))
        for _ in range(2):
            run = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120, remove_created_files=True))
            assert run.outcome is Outcome.COMPLETED, run.output


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_each_mutant_run_starts_in_the_root_without_what_earlier_runs_left(tmp_path, options):
    """The origin check and every mutant run in the root as the normal run
    does (the clone's for a worker), first without the files earlier runs
    created there, with an empty temporary directory of their own. The session
    leaves the prepared copy as it found it."""
    prepared = _prepared(tmp_path)
    with prepared.use() as context:
        adapter = lb.create("python", context)
        suite = lb.Suite(str(Path(context.root) / "tests"), ("test_arith.py",))
        with adapter.open_mutation_session(**options) as session:
            run = session.run(MutationRequest(
                target="arith.py", suite=suite,
                catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
        assert run.outcome is Outcome.COMPLETED, run.reason
        assert run.score() == 100.0
        assert not (Path(context.root) / "left.txt").exists()


READ_ONLY = (
    "import os\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_cannot_write_a_read_only_file(tmp_path):\n"
    "    path = tmp_path / 'locked.txt'\n"
    "    path.write_text('x')\n"
    "    path.chmod(0o444)\n"
    "    assert not os.access(path, os.W_OK)\n")


@pytest.mark.mutation_tool
@pytest.mark.skipif(os.name == "nt" or not _cosmic_ray_runs(),
                    reason="needs POSIX permissions and cosmic-ray beside the interpreter")
def test_a_mutant_run_has_the_normal_runs_permissions(tmp_path):
    """No user namespace: under one the tests ran as uid 0, which may write a
    read-only file, so a test the normal run passed failed the origin check."""
    adapter, suite = _project(tmp_path, READ_ONLY)
    assert adapter.run_suite(SuiteRunRequest(suite, timeout_s=120)).outcome is Outcome.COMPLETED
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.COMPLETED, run.reason


# Increment D: one pytest command, with the context's interpreter flags,
# the suite's configuration, rootdir and confcutdir.

DEV_MODE = (
    "import sys\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_runs_with_the_contexts_interpreter_flags():\n"
    "    assert sys.flags.dev_mode\n")


def _flagged_project(tmp_path, tests, flags):
    adapter, suite = _project(tmp_path, tests)
    context = adapter.context
    return PythonAdapter(lb.ProjectContext(
        root=context.root, interpreter=context.interpreter,
        interpreter_flags=flags)), suite


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_cosmic_ray_runs_have_the_contexts_interpreter_flags(tmp_path, options):
    """The context's interpreter flags (Testuary's ``-X frozen_modules=off``)
    reach every run, Cosmic Ray's too, which started pytest without them."""
    adapter, suite = _flagged_project(tmp_path, DEV_MODE, ("-X", "dev"))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert normal.outcome is Outcome.COMPLETED, normal.output
    with adapter.open_mutation_session(**options) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_cosmic_ray_runs_read_only_the_suites_configuration(tmp_path, options):
    """A pytest configuration above a project without one applies to no run:
    the normal run names its anchor with -c, and so does each mutant's run,
    where pytest found the outer one (here collecting no test, so the origin
    check refused the suite) and took the conftest files below it."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "pytest.ini").write_text("[pytest]\npython_functions = check_*\n",
                                      encoding="utf-8")
    adapter, suite = _project(outer, ENVIRONMENT)
    (Path(adapter.context.root) / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n    items.clear()\n", encoding="utf-8")
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert normal.outcome is Outcome.COMPLETED and normal.counts.passed == 2, normal.output
    with adapter.open_mutation_session(**options) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0
    # The session removes the anchor it wrote beside the suite.
    assert not (Path(suite.root) / "pytest.ini").exists()


def test_the_collection_repair_reads_only_the_suites_configuration(tmp_path, monkeypatch):
    """The collection repair's pytest command is the normal run's too: the
    candidate's directory anchored with -c, as rootdir and confcutdir."""
    from langadapter.python._run_definition import DISABLED_PLUGINS, pytest_arguments

    adapter, _suite = _project(tmp_path, ENVIRONMENT)
    executor = adapter._executor()
    seen = []

    class Recorded:
        def __getattr__(self, name):
            return getattr(executor, name)

        def run(self, command, **kwargs):
            seen.append(list(command))
            return executor.run(command, **kwargs)

    monkeypatch.setattr(adapter, "_executor", lambda: Recorded())
    adapter.sources.prune_collection_breakers("def test_one():\n    assert True\n",
                                              timeout_s=120)
    (command,) = [c for c in seen if "--collect-only" in c]
    config = command[command.index("-c") + 1]
    rootdir = next(a for a in command if a.startswith("--rootdir=")).split("=", 1)[1]
    assert Path(config) == Path(rootdir) / "pytest.ini"
    assert command[command.index("--collect-only") + 1:] == pytest_arguments(
        config, rootdir, traceback="auto", disabled_plugins=DISABLED_PLUGINS)


# The PYTHONPATH row: a worker's import path holds its clone in place of the
# project, without the original tree.

TWO_FUNCTIONS = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
ONE_TREE = (
    "import os\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_a_clone_sees_no_other_tree():\n"
    "    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
    "    with open(os.path.join(here, 'origin.txt'), encoding='utf-8') as handle:\n"
    "        original = handle.read()\n"
    "    entries = [os.path.normcase(os.path.realpath(entry))\n"
    "               for entry in os.environ.get('PYTHONPATH', '').split(os.pathsep) if entry]\n"
    "    if os.path.normcase(os.path.realpath(here)) != original:\n"
    "        assert original not in entries\n")


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
def test_a_workers_runs_see_their_clone_in_place_of_the_project(tmp_path):
    """A worker's PYTHONPATH held the project root after its clone, so a
    test reading the import path (or a namespace package) saw both trees:
    here every mutant was killed, the one in sub too, which no test calls."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "arith.py").write_text(TWO_FUNCTIONS, encoding="utf-8")
    (root / "origin.txt").write_text(os.path.normcase(os.path.realpath(root)),
                                     encoding="utf-8")
    (root / "tests" / "test_arith.py").write_text(ONE_TREE, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root / "tests"), ("test_arith.py",))
    with adapter.open_mutation_session(workers=2, distributor="http") as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",
                                           "core/ReplaceBinaryOperator_Sub_Add"))))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 50.0


def test_the_engine_and_the_container_runner_move_the_same_entries(tmp_path):
    from langadapter.python import _cr_session_runner as runner

    project, clone, other = tmp_path / "proj", tmp_path / "clone", tmp_path / "other"
    pythonpath = os.pathsep.join([str(project / "src"), str(project), str(other), ""])
    expected = os.pathsep.join([str(clone / "src"), str(clone), str(other)])
    for module in (engine_module, runner):
        assert module._clone_pythonpath(pythonpath, project, clone, ("src",)) == expected


# The instrument: the suite's setup files carry it into every run, Cosmic
# Ray's too, and the mutation result records it as the suite run does.

@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
def test_a_mutation_run_records_the_instrument_its_suite_carries(tmp_path):
    """The mutation result's identity named no instrument although every
    mutant's run had the one the suite's setup file carries."""
    adapter, _suite = _project(tmp_path, ENVIRONMENT)
    tests = Path(adapter.context.root) / "tests"
    assert adapter.prepare_suite(str(tests)).outcome is Outcome.COMPLETED
    conftest = tests / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + CARRIED_INSTRUMENT,
                        encoding="utf-8")
    suite = lb.Suite(str(tests), ("test_arith.py",), setup_files=("conftest.py",))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert normal.outcome is Outcome.COMPLETED, normal.output
    assert normal.side_effects.instrument is not None
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.identity.observation.instrument == normal.side_effects.instrument


# The origin check runs where the first mutants run (a worker's clone) and
# names the tests that stopped it.

TWO_FAILING = (
    "from arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_first_red():\n    assert add(2, 2) == 5\n\n\n"
    "class TestMore:\n"
    "    def test_second_red(self):\n        assert add(1, 1) == 3\n")


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_a_refused_suite_names_every_test_that_stopped_it(tmp_path, options):
    """The origin check's failing tests, from its JUnit report, every one of
    them (the check stops at no first failure), as the suite's test ids."""
    adapter, suite = _project(tmp_path, TWO_FAILING)
    with adapter.open_mutation_session(**options) as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",))))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert sorted(test.text for test in run.stopping_tests) == [
        "test_arith.py::TestMore::test_second_red", "test_arith.py::test_first_red"]
    assert run.to_kill_matrix()["stopping_tests"] == [
        test.text for test in run.stopping_tests]


IN_ITS_PROJECT_ONLY = (
    "import os\n\nfrom arith import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_runs_in_the_project_itself():\n"
    "    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
    "    with open(os.path.join(here, 'origin.txt'), encoding='utf-8') as handle:\n"
    "        assert os.path.normcase(os.path.realpath(here)) == handle.read()\n")


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")
def test_the_origin_check_runs_in_a_workers_clone(tmp_path):
    """With workers, the check ran in the project, where this suite passes,
    and every mutant was then killed in the clones, sub's untested one too.
    Run in a clone, the check refuses the suite and names the test."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "arith.py").write_text(TWO_FUNCTIONS, encoding="utf-8")
    (root / "origin.txt").write_text(os.path.normcase(os.path.realpath(root)),
                                     encoding="utf-8")
    (root / "tests" / "test_arith.py").write_text(IN_ITS_PROJECT_ONLY, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root / "tests"), ("test_arith.py",))
    assert adapter.run_suite(SuiteRunRequest(suite, timeout_s=120)).outcome is Outcome.COMPLETED
    with adapter.open_mutation_session(workers=2, distributor="http") as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",
                                           "core/ReplaceBinaryOperator_Sub_Add"))))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE, run.score()
    assert [test.text for test in run.stopping_tests] == [
        "test_arith.py::test_runs_in_the_project_itself"]
