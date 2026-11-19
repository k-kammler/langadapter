"""The Python adapter's public mutation interface with real Cosmic Ray and
pytest: sessions with an explicit lifetime, settings recorded with their
sources, self-contained results with the evidence behind each verdict, and
failed runs without scores."""
import subprocess
import sys
import threading

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter import mutation as ev
from langadapter.mutation import (
    MutationClass, DEFAULT_IMPLEMENTATION, BudgetedSuite, PerSuiteRequest, _verdict_of,
    Catalogue, EffectiveSetting, MutationRequest, TimeoutRule, FAILED_INITIALIZATION,
    FAILED_INSTRUMENT_CHECK, FAILED_SESSION, FAILED_WORKERS,
)
from langadapter.outcomes import Outcome
from langadapter.python import _cosmic_ray as engine_module
from langadapter.python._cosmic_ray import MutationSession
from langadapter.python.adapter import PythonAdapter

TARGET = "def add(a, b):\n    return a + b\n"
STRONG = ("from arith import add\n\n\n"
          "def test_strong():\n    assert add(2, 3) == 5\n")
WEAK = ("from arith import add\n\n\n"
        "def test_weak():\n    add(1, 1)\n")
ADD_SUB = ["core/ReplaceBinaryOperator_Add_Sub"]


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter")


def _project(tmp_path, *tests):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "arith.py").write_text(TARGET, encoding="utf-8")
    names = []
    for name, text in tests:
        (root / "tests" / name).write_text(text, encoding="utf-8")
        names.append(name)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, root, lb.Suite(str(root / "tests"), tuple(names))


def test_session_uses_the_contexts_explicit_executor(tmp_path, monkeypatch):
    calls = []

    class Executor:
        def run(self, command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ready", "")

    class Session:
        use_http = False

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    def open_session(engine, **kwargs):
        engine._executor().run(["project-executor-probe"])
        return Session()

    monkeypatch.setattr(engine_module.CosmicRayEngine, "session", open_session)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)),
        executor=Executor())
    with adapter.open_mutation_session() as session:
        assert session.outcome is Outcome.COMPLETED
    assert calls == [["project-executor-probe"]]


RED = ("from arith import add\n\n\n"
       "def test_red():\n    assert add(1, 1) == 3\n")


@pytest.mark.mutation_tool
def test_an_unrelated_captured_pythonpath_keeps_the_project_importable(tmp_path):
    """A captured PYTHONPATH keeps the project root on the per-mutant runs'
    import path, so every origin check finds the project's module."""
    import os

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _, root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        environment=lb.Environment(dict(os.environ, PYTHONPATH=str(unrelated)),
                                   inherit=False)))
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


@pytest.mark.mutation_tool
def test_a_refused_suite_leaves_the_session_to_the_next(tmp_path):
    """A refused origin check fails only its own run: the session's later
    runs, other suites of a per-suite measurement included, run as usual."""
    adapter, root, _ = _project(tmp_path, ("test_red.py", RED), ("test_strong.py", STRONG))
    red = lb.Suite(str(root / "tests"), ("test_red.py",))
    strong = lb.Suite(str(root / "tests"), ("test_strong.py",))
    with adapter.open_mutation_session() as session:
        refused = session.run(MutationRequest(target="arith.py", suite=red))
        measured = session.run(MutationRequest(target="arith.py", suite=strong))
    assert refused.failure == FAILED_INSTRUMENT_CHECK
    assert "did not pass" in refused.reason
    assert measured.outcome is Outcome.COMPLETED, measured.reason
    assert measured.score() == 100.0


@pytest.mark.mutation_tool
def test_an_ordinary_run_records_every_setting_as_the_engines_default(
        tmp_path):
    adapter, root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    assert adapter.mutation_engines() == ("cosmic_ray",)
    with adapter.open_mutation_session() as session:
        assert session.is_open and session.workers_started == 1
        run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert not session.is_open
    assert run.outcome is Outcome.COMPLETED and run.complete
    assert {s.source for s in run.settings.values()} == {"default"}
    assert run.settings["per_mutant_timeout"] == EffectiveSetting(
        10, "default", {"factor": 1.5, "minimum_s": 10, "cap_s": None,
                        "passing_suite_s": None})
    assert run.settings["wall_clock_limit"].value == 3600
    assert run.settings["attribution"].value == "first"
    assert run.counts()[MutationClass.DETECTED] > 0
    assert run.score() == 100.0
    identity = run.identity.to_dict()
    assert identity["target"]["path"] == "arith.py"
    assert identity["suite"] == [{"path": "test_strong.py",
                                  "sha256": suite.identity()[0].sha256}]
    assert identity["catalogue"] == "full" and identity["workers"] == 1
    assert "cosmic-ray" in identity["toolchain"]["tools"]
    # Serialized after the session closed: complete, and it names its suite.
    matrix = run.to_kill_matrix()
    assert matrix["suite"] == identity["suite"]
    assert len(matrix["mutants"]) == matrix["summary"]["total"] > 0
    # The matrix records where the run executed and, on Linux, its supervision.
    assert matrix["execution"] == identity["execution"]
    assert matrix["execution"]["kind"] == "local"
    if sys.platform == "linux":
        assert matrix["execution"]["supervision"] in ("pidfd", "process_group")
    else:
        assert "supervision" not in matrix["execution"]


@pytest.mark.mutation_tool
def test_a_session_on_a_new_adapter_runs_with_the_projects_own_pytest_configuration(tmp_path):
    """A session on an adapter that has run nothing yet, with the suite
    rooted at the project directory, runs with the project's pytest
    configuration (pyproject.toml's pythonpath here), as one after a suite
    run does: a measuring run does not depend on what ran before on the
    adapter. No anchoring pytest.ini is left in the project for later runs."""
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "arith.py").write_text(TARGET, encoding="utf-8")
    (root / "tests" / "test_arith.py").write_text(
        "from pkg.arith import add\n\n\ndef test_strong():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n',
                                         encoding="utf-8")
    suite = lb.Suite(str(root), ("tests/test_arith.py",))
    request = MutationRequest("src/pkg/arith.py", suite,
                              catalogue=Catalogue(operators=tuple(ADD_SUB)))
    for _ in range(2):
        adapter = PythonAdapter(lb.ProjectContext(
            root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
        with adapter.mutations.open_session("cosmic_ray") as session:
            run = session.run(request)
        assert run.outcome is Outcome.COMPLETED and run.complete, run.reason
        assert run.score() == 100.0
        assert not (root / "pytest.ini").exists()


@pytest.mark.mutation_tool
def test_a_local_context_with_a_work_dir_reads_its_own_session(tmp_path):
    """A local run keeps its session in the engine's own directory. The work
    directory's session path is a container run's."""
    _, root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    zone = tmp_path / "zone"
    zone.mkdir()
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(zone))))
    request = MutationRequest("arith.py", suite, catalogue=Catalogue(operators=tuple(ADD_SUB)))
    with adapter.mutations.open_session("cosmic_ray") as session:
        run = session.run(request)
    assert run.outcome is Outcome.COMPLETED and run.complete, run.reason
    assert run.score() == 100.0
    assert not (zone / "cr").exists()


@pytest.mark.mutation_tool
def test_a_verbose_session_runs_and_logs_the_tools_output(tmp_path):
    from langadapter.python.cosmic_ray import CosmicRaySettings

    adapter, _, suite = _project(tmp_path, ("test_strong.py", STRONG))
    logged = []
    settings = CosmicRaySettings(verbose=True, log=lambda message, *args, **kwargs: logged.append(message))
    request = MutationRequest("arith.py", suite, catalogue=Catalogue(operators=tuple(ADD_SUB)))
    with adapter.mutations.open_session("cosmic_ray", settings=settings) as session:
        run = session.run(request)
    assert run.outcome is Outcome.COMPLETED and run.complete, run.reason
    assert run.score() == 100.0
    assert any("verbose exec output" in message for message in logged)
    assert not any("No such option" in message for message in logged)


@pytest.mark.mutation_tool
def test_an_exec_that_writes_more_than_a_pipe_holds_completes(tmp_path, monkeypatch):
    """exec's output is read while it runs, so an exec that writes more than
    its pipes hold (the verbose log of many mutants) finishes instead of
    stalling until the wall clock ends it."""
    adapter, _, suite = _project(tmp_path, ("test_strong.py", STRONG))
    chatty = tmp_path / "chatty.py"
    chatty.write_text(
        "import subprocess, sys\n"
        "if 'exec' in sys.argv:\n"
        "    sys.stdout.write('x' * 1000000)\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.write('y' * 1000000)\n"
        "    sys.stderr.flush()\n"
        "sys.exit(subprocess.run(sys.argv[1:]).returncode)\n", encoding="utf-8")
    start = engine_module._process_tree.start
    monkeypatch.setattr(engine_module._process_tree, "start", lambda command, **options: start(
        [sys.executable, str(chatty), *command], **options))
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest("arith.py", suite, wall_clock_limit_s=60,
                                          catalogue=Catalogue(operators=tuple(ADD_SUB))))
    assert run.outcome is Outcome.COMPLETED and run.complete, run.reason
    assert run.score() == 100.0


@pytest.mark.mutation_tool
def test_group_measure_preserves_native_verdicts(tmp_path):
    adapter, _, suite = _project(tmp_path, ("test_strong.py", STRONG))
    request = MutationRequest("arith.py", suite, catalogue=Catalogue(operators=tuple(ADD_SUB)))
    with adapter.open_mutation_session() as session:
        flat = session.run(request)
    with adapter.mutations.open_session("cosmic_ray") as session:
        measured = session.run(request)

    def verdicts(run):
        return [(mutant.key, mutant.classification, mutant.killers, mutant.killing_tests)
                for mutant in run.mutants]
    assert measured.outcome is Outcome.COMPLETED and measured.complete, measured.reason
    assert measured.score() == flat.score() == 100.0
    assert measured.identity == flat.identity
    assert verdicts(measured) == verdicts(flat)
    assert measured.resolution.requested == "measure" and measured.resolution.method == "measured"


@pytest.mark.mutation_tool
def test_a_consumers_timeout_rule_is_recorded_as_requested(tmp_path):
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    rule = TimeoutRule(factor=10, minimum_s=30, cap_s=900)
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite, catalogue=Catalogue(
                operators=tuple(ADD_SUB)),
            per_mutant_timeout=rule, passing_suite_s=2.5, attribution="all"))
    assert run.settings["per_mutant_timeout"] == EffectiveSetting(
        30, "request", {"factor": 10, "minimum_s": 30, "cap_s": 900,
                        "passing_suite_s": 2.5})
    assert run.settings["attribution"] == EffectiveSetting("all", "request")
    assert run.settings["catalogue"].source == "request"
    assert run.identity.per_mutant_timeout_s == 30
    assert run.identity.operators == tuple(ADD_SUB)
    (mutant,) = run.mutants
    # pytest prints the file relative to the run's scratch directory, and the
    # run identifies the test within the suite.
    assert [str(test) for test in mutant.killing_tests] == [
        "test_strong.py::test_strong"]
    assert mutant.killers[0].endswith("test_strong.py::test_strong")


@pytest.mark.mutation_tool
def test_a_suite_runs_as_its_named_files(tmp_path):
    adapter, root, _ = _project(tmp_path, ("test_strong.py", STRONG),
                                ("test_weak.py", WEAK))
    weak_only = lb.Suite(str(root / "tests"), ("test_weak.py",))
    both = lb.Suite(str(root / "tests"), ("test_weak.py", "test_strong.py"))
    with adapter.open_mutation_session() as session:
        alone = session.run(MutationRequest(
            target="arith.py", suite=weak_only,
            catalogue=Catalogue(operators=tuple(ADD_SUB))))
        together = session.run(MutationRequest(
            target="arith.py", suite=both,
            catalogue=Catalogue(operators=tuple(ADD_SUB))))
    assert [m.classification.mutation_class for m in alone.mutants] == [
        MutationClass.UNDETECTED]
    assert [m.classification.mutation_class for m in together.mutants] == [
        MutationClass.DETECTED]


def test_a_tool_that_cannot_start_leaves_no_score_and_no_counts(
        tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    # The command line comes from the project Python's profile (on 3.14
    # the interpreter runs it), so the stand-in replaces the command.
    monkeypatch.setattr(engine_module.CosmicRayEngine, "_cosmic_ray_command",
                        lambda self, cosmic_ray, python=None: [str(tmp_path / "no-tool")])
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.reason.startswith("the mutation tool's initialization failed: ")
    assert "FileNotFoundError" in run.reason
    assert run.failure == FAILED_INITIALIZATION
    assert run.counts() is None and run.score() is None


#: A stand-in for Cosmic Ray's command line: init writes a catalogue of one
#: mutant and exits {init_exit}, exec measures nothing and exits {exec_exit}.
STAND_IN = """\
import sqlite3
import sys

if "init" in sys.argv:
    conn = sqlite3.connect(sys.argv[-1])
    conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
                 "operator_name TEXT, occurrence INTEGER, start_pos_row INTEGER, "
                 "start_pos_col INTEGER)")
    conn.execute("CREATE TABLE work_results (job_id TEXT, worker_outcome TEXT, "
                 "output TEXT, test_outcome TEXT, diff TEXT)")
    conn.execute("INSERT INTO work_items VALUES ('j1')")
    conn.execute("INSERT INTO mutation_specs VALUES ('j1', 'arith.py', 'core/X', 0, 2, 13)")
    conn.commit()
    conn.close()
    sys.exit({init_exit})
sys.exit({exec_exit})
"""


@pytest.mark.parametrize(("init_exit", "exec_exit", "refuse_exec", "failure", "reason"), [
    (1, 0, False, FAILED_INITIALIZATION,
     "the mutation tool's initialization failed: cosmic-ray init exited 1"),
    (0, 0, True, FAILED_SESSION,
     "the mutation tool's exec did not start (PermissionError: exec refused)"),
    (0, 1, False, FAILED_SESSION,
     "the mutation tool's exec exited 1 with 1 of 1 mutants untested"),
])
def test_a_failed_step_of_the_tool_fails_the_run_with_its_kind(
        tmp_path, monkeypatch, init_exit, exec_exit, refuse_exec, failure, reason):
    """An init that fails after writing its catalogue, an exec that does not
    start and an exec that fails with mutants untested each fail the run,
    with no score and the step's failure kind, never a completed run."""
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    tool = tmp_path / "stand_in_tool.py"
    tool.write_text(STAND_IN.format(init_exit=init_exit, exec_exit=exec_exit),
                    encoding="utf-8")
    monkeypatch.setattr(engine_module.CosmicRayEngine, "_cosmic_ray_command",
                        lambda self, cosmic_ray, python=None: [sys.executable, str(tool)])
    start = engine_module._process_tree.start

    def refusing(args, **options):
        if refuse_exec and "exec" in [str(arg) for arg in args]:
            raise PermissionError("exec refused")
        return start(args, **options)

    monkeypatch.setattr(engine_module._process_tree, "start", refusing)
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert (run.outcome, run.failure, run.reason) == (
        Outcome.INFRASTRUCTURE_FAILURE, failure, reason)
    assert run.score() is None
    assert run.mutants == ()


@pytest.mark.parametrize("worker", ["NORMAL", "EXCEPTION"])
def test_a_run_whose_every_result_is_incompetent_is_no_measurement(
        tmp_path, monkeypatch, worker):
    """Cosmic Ray records INCOMPETENT when running the tests (worker NORMAL)
    or applying the mutation (worker EXCEPTION) raised. With it for every
    mutant nothing was measured: the run fails with the workers' failure
    kind and no score, never a completed run that counts them as killed."""
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    tool = tmp_path / "stand_in_tool.py"
    records = ("\nconn = sqlite3.connect(sys.argv[-1])\n"
               "conn.execute(\"INSERT INTO work_results VALUES ('j1', "
               f"'{worker}', 'Traceback (most recent call last)', 'INCOMPETENT', '')\")\n"
               "conn.commit()\nconn.close()\n")
    tool.write_text(STAND_IN.format(init_exit=0, exec_exit=0).replace("\nsys.exit(0)\n", records),
                    encoding="utf-8")
    monkeypatch.setattr(engine_module.CosmicRayEngine, "_cosmic_ray_command",
                        lambda self, cosmic_ray, python=None: [sys.executable, str(tool)])
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert (run.outcome, run.failure) == (Outcome.INFRASTRUCTURE_FAILURE, FAILED_WORKERS), (
        run.reason)
    assert run.reason.endswith(("so none was measured",
                                "so none was measured: Traceback (most recent call last)"))
    assert run.score() is None and run.mutants == ()


@pytest.mark.mutation_tool
def test_fewer_workers_than_asked_fail_the_session_unless_allowed(
        tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    real_wait = engine_module.CosmicRayEngine._wait_for_worker
    refused = []
    lock = threading.Lock()

    def one_worker_never_answers(port, timeout=30.0):
        with lock:
            if not refused:
                refused.append(port)
                return False
        return real_wait(port, timeout)

    monkeypatch.setattr(engine_module.CosmicRayEngine, "_wait_for_worker",
                        staticmethod(one_worker_never_answers))
    session = adapter.open_mutation_session(workers=2, distributor="http")
    assert session.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert session.reason.startswith("1 of 2 workers started")
    assert not session.is_open
    run = session.run(MutationRequest(target="arith.py", suite=suite))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.score() is None
    assert run.failure == FAILED_SESSION
    refused.clear()
    with adapter.open_mutation_session(
            workers=2, distributor="http",
            allow_fewer_workers=True) as allowed:
        assert allowed.outcome is Outcome.COMPLETED
        assert allowed.workers_started == 1
        assert allowed.reason == "1 of 2 workers started"


@pytest.mark.mutation_tool
def test_the_wall_clock_ends_a_run_incomplete_without_a_score(tmp_path):
    slow = ("import time\n\nfrom arith import add\n\n\n"
            "def test_slow():\n    time.sleep(3)\n    assert add(2, 3) == 5\n")
    adapter, _root, suite = _project(tmp_path, ("test_slow.py", slow))
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite, wall_clock_limit_s=2,
            catalogue=Catalogue(operators=(
                "core/ReplaceBinaryOperator_Add_Sub",
                "core/ReplaceBinaryOperator_Add_Mul",
                "core/ReplaceBinaryOperator_Add_Div"))))
    assert run.outcome is Outcome.TIMED_OUT and not run.complete
    assert run.score() is None
    assert run.incomplete["untested"] >= 1
    assert "wall-clock limit of 2 s" in run.reason


@pytest.mark.parametrize("failing", [
    "def test_fails():\n    assert False, 'the suite fails unmutated'\n",
    "import missing_module_of_the_suite\n\n\ndef test_never_collected():\n    pass\n"],
    ids=["failing-test", "collection-error"])
def test_a_suite_that_fails_unmutated_is_refused_without_a_score(tmp_path, failing):
    """A suite failing unmutated would kill every mutant with no named
    killer, so the run is refused."""
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG),
                                     ("test_failing.py", failing))
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite, catalogue=Catalogue(operators=tuple(ADD_SUB))))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.score() is None
    assert run.failure == FAILED_INSTRUMENT_CHECK and run.mutants == ()
    assert run.reason.startswith("the unmutated suite did not pass in the check run")
    # The run's own account of its end follows: pytest's summary.
    status, named = (1, "test_failing.py::test_fails") if failing.startswith("def") else (
        2, "ERROR")
    assert f"(pytest exit status {status})" in run.reason and named in run.reason


def test_a_check_runs_refusal_names_the_original_projects_paths(tmp_path):
    """The refusal names the prepared copy's paths as the original
    project's, as a suite run's text does. A setup file that fails at
    startup names its own path."""
    from pathlib import Path

    from langadapter.preparation import prepare_project

    adapter, root, _suite = _project(tmp_path, ("test_strong.py", STRONG))
    (root / "tests" / "conftest.py").write_text(
        "raise RuntimeError('broken setup at ' + __file__)\n", encoding="utf-8")
    owner = prepare_project(adapter.context)
    try:
        with owner.use() as scoped:
            copy = Path(scoped.root)
            with lb.create("python", scoped).open_mutation_session() as session:
                run = session.run(MutationRequest(
                    target=str(copy / "arith.py"),
                    suite=lb.Suite(str(copy / "tests"), ("test_strong.py",), ("conftest.py",)),
                    catalogue=Catalogue(operators=tuple(ADD_SUB))))
    finally:
        owner.close()
    assert run.score() is None and "broken setup at" in run.reason
    assert str(copy) not in run.reason
    assert str(root / "tests" / "conftest.py") in run.reason


@pytest.mark.mutation_tool
def test_unresolved_kills_fail_a_run_that_asks_for_it(tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))

    def leave_one_unresolved(self, **kwargs):
        self._last_unresolved_job_ids = {"a-kill-without-evidence"}
        self._last_verify_checked = 1

    monkeypatch.setattr(engine_module.CosmicRayEngine,
                        "_verify_unattributed_kills", leave_one_unresolved)
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite, fail_on_unresolved=True,
            catalogue=Catalogue(operators=tuple(ADD_SUB))))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.reason.startswith("unresolved kills fail this run")
    assert run.failure == FAILED_INSTRUMENT_CHECK
    assert run.settings["fail_on_unresolved"] == EffectiveSetting(
        True, "request")


def test_invalid_requests_name_their_field(tmp_path):
    adapter, root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    (root / "tests" / "sub").mkdir()
    (root / "tests" / "sub" / "test_strong.py").write_text(STRONG,
                                                          encoding="utf-8")
    with pytest.raises(InvalidRequest) as refused:
        adapter.open_mutation_session(engine="mutmut")
    assert refused.value.field == "engine"
    with pytest.raises(InvalidRequest) as refused:
        adapter.open_mutation_session(workers=2, distributor="local")
    assert refused.value.field == "workers"
    with adapter.open_mutation_session() as session:
        for request, field in (
                (MutationRequest(target="arith.py", suite=suite,
                                 per_mutant_timeout=-1), "per_mutant_timeout"),
                (MutationRequest(target="arith.py", suite=suite,
                                 attribution="some"), "attribution"),
                (MutationRequest(target=str(tmp_path), suite=suite),
                 "target"),
                (MutationRequest(target="arith.py", suite=suite, workers=2),
                 "workers"),
                (MutationRequest(target="arith.py", suite=lb.Suite(
                    str(root / "tests"),
                    ("test_strong.py", "sub/test_strong.py"))),
                 "suite.test_files"),
                (MutationRequest(target="arith.py", suite=suite,
                                 catalogue=Catalogue(name="nonexistent")),
                 "catalogue.name")):
            with pytest.raises(InvalidRequest) as refused:
                session.run(request)
            assert refused.value.field == field


def test_a_target_outside_the_project_is_refused_before_any_run(tmp_path, monkeypatch):
    """As Java's: a target file outside the project, named by an absolute
    path or one that leaves the root, is refused before Cosmic Ray is
    configured, and the file stays as it is."""
    adapter, _root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    outside = tmp_path / "outside.py"
    outside.write_text(TARGET, encoding="utf-8")
    configured = []
    monkeypatch.setattr(engine_module.CosmicRayEngine, "_write_config_toml",
                        lambda self, *args, **kwargs: configured.append(args))
    with adapter.open_mutation_session() as session:
        for target in (str(outside), "../outside.py"):
            with pytest.raises(InvalidRequest) as refused:
                session.run(MutationRequest(target=target, suite=suite,
                                            catalogue=Catalogue(operators=tuple(ADD_SUB))))
            assert refused.value.field == "target"
            assert "does not lie under the project root" in refused.value.message
    assert configured == []
    assert outside.read_text(encoding="utf-8") == TARGET


CRASHES = ("import os\n\n\n"
           "def check(x):\n"
           "    if x > 0:\n"
           "        return x\n"
           "    os._exit(3)\n")
USES_CHECK = ("from crash import check\n\n\n"
              "def test_positive():\n    assert check(1) == 1\n")
UNRELATED = "def test_unrelated():\n    assert 1 + 1 == 2\n"


#: ``n - 1`` made ``n + 1`` makes countdown loop in test_counts_down.
LOOPS = ("def countdown(n):\n    while n > 0:\n        n = n - 1\n"
         "    return n\n")
COUNTS = ("from loop import countdown\n\n\n"
          "def test_counts_down():\n    assert countdown(3) == 0\n")
SUB_ADD = ("core/ReplaceBinaryOperator_Sub_Add",)


def _loop_run(tmp_path, *, workers=1, **request):
    adapter, root, suite = _project(tmp_path, ("test_loop.py", COUNTS))
    (root / "loop.py").write_text(LOOPS, encoding="utf-8")
    options = ({"workers": workers, "distributor": "http"} if workers > 1
               else {})
    with adapter.open_mutation_session(**options) as session:
        return session.run(MutationRequest(
            target="loop.py", suite=suite, per_mutant_timeout=10,
            catalogue=Catalogue(operators=SUB_ADD), **request))


def _timeout_readings(mutant):
    return [(item.label, item.tests) for item in mutant.evidence
            if isinstance(item, ev.TimeoutEvidence)]


@pytest.mark.mutation_tool
@pytest.mark.parametrize("workers", [1, 2])
def test_a_suite_without_the_instrument_journals_through_the_plugin(
        tmp_path, workers):
    """The journal is on by default, and a suite without the instrument
    journals through the adapter's plugin: the looping mutant keeps its
    timeout with the test it hung in, in place (one worker) and in the
    workers' clones (two)."""
    run = _loop_run(tmp_path, workers=workers)
    assert run.settings["timeout_journal"] == EffectiveSetting(
        True, "default")
    (mutant,) = run.mutants
    assert mutant.status == "timeout", mutant.row
    ((label, tests),) = _timeout_readings(mutant)
    assert label == ev.HANG
    assert len(tests) == 1 and tests[0].endswith("test_counts_down")


@pytest.mark.mutation_tool
def test_a_request_can_turn_the_journal_off(tmp_path):
    run = _loop_run(tmp_path, timeout_journal=False)
    assert run.settings["timeout_journal"] == EffectiveSetting(
        False, "request")
    (mutant,) = run.mutants
    assert mutant.status == "timeout", mutant.row
    assert _timeout_readings(mutant) == []


def test_a_suite_carrying_the_instrument_takes_no_plugin(tmp_path, carry_instrument):
    """The instrument's hooks write the journal. The plugin beside them
    would write every line twice. A setup file counts whether the request
    lists it or pytest loads it for the test files."""
    adapter, root, suite = _project(tmp_path, ("test_strong.py", STRONG))
    carries = MutationSession._carries_instrument
    assert not carries(MutationRequest(target="arith.py", suite=suite))
    carry_instrument(root / "tests")
    assert carries(MutationRequest(target="arith.py", suite=suite))
    listed = lb.Suite(suite.root, suite.test_files, ("conftest.py",))
    assert carries(MutationRequest(target="arith.py", suite=listed))


@pytest.mark.mutation_tool
def test_a_crash_kill_is_verified_and_attributed_to_its_file(tmp_path):
    adapter, root, suite = _project(tmp_path, ("test_uses.py", USES_CHECK),
                                    ("test_unrelated.py", UNRELATED))
    (root / "crash.py").write_text(CRASHES, encoding="utf-8")
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="crash.py", suite=suite, catalogue=Catalogue(
                operators=("core/ReplaceComparisonOperator_Gt_Lt",))))
    # The mutant ends the test process before pytest reports, a kill naming
    # no test. Both re-runs die the same way, so it stays unresolved, and the
    # file that kills it alone is recorded.
    (mutant,) = run.mutants
    assert mutant.status == "killed"
    verification, attribution = mutant.evidence
    assert verification.verdict == ev.UNRESOLVED
    assert not verification.recorded.reported
    assert [a.test_outcome for a in verification.attempts] == [
        "KILLED", "KILLED"]
    assert attribution == ev.FileAttribution(
        ev.ATTRIBUTED, (("test_unrelated.py", False), ("test_uses.py", True)))
    assert mutant.killing_tests == ()
    assert "unresolved" not in mutant.row
    with pytest.raises(InvalidRequest) as refused:
        run.derive_for_tests([lb.TestId("test_uses.py", "test_positive")],
                             covered_lines=[5])
    assert refused.value.field == "run"


@pytest.mark.mutation_tool
def test_a_transient_crash_is_replaced_by_the_verifications_measurement(
        tmp_path):
    # The session's origin run (the import check) is the suite's first run,
    # so the crash comes with the second, the mutant's.
    runs = tmp_path / "runs"
    flaky = ("import os\nfrom pathlib import Path\n\nfrom arith import add\n\n"
             f"RUNS = Path({str(runs)!r})\n\n\n"
             "def test_weak_after_one_crash():\n"
             "    count = int(RUNS.read_text(encoding='utf-8')) + 1 if RUNS.exists() else 1\n"
             "    RUNS.write_text(str(count), encoding='utf-8')\n"
             "    if count == 2:\n"
             "        os._exit(3)\n"
             "    add(1, 1)\n")
    adapter, _root, suite = _project(tmp_path, ("test_flaky.py", flaky))
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="arith.py", suite=suite,
            catalogue=Catalogue(operators=tuple(ADD_SUB))))
    (mutant,) = run.mutants
    (verification,) = mutant.evidence
    assert (verification.verdict, verification.replaced) == (
        ev.CORRECTED, True)
    assert verification.recorded.test_outcome == "KILLED"
    assert not verification.recorded.reported
    (attempt,) = verification.attempts
    assert (attempt.test_outcome, attempt.reported) == ("SURVIVED", True)
    assert mutant.status == "survived"
    assert run.score() == 0.0


@pytest.mark.mutation_tool
def test_per_suite_verdicts_equal_each_suites_standalone_run(tmp_path):
    adapter, root, _ = _project(tmp_path, ("test_strong.py", STRONG),
                                ("test_weak.py", WEAK))
    tests = str(root / "tests")
    request = PerSuiteRequest("arith.py", (
        BudgetedSuite("strong", lb.Suite(tests, ("test_strong.py",)), 10),
        BudgetedSuite("weak", lb.Suite(tests, ("test_weak.py",)), 10)),
        catalogue=Catalogue(operators=tuple(ADD_SUB)))
    operation = next(item for item in adapter.operation_capabilities()
                     if item.name == "mutations.per_suite")
    assert operation.supported, operation.reason
    with adapter.mutations.open_session("cosmic_ray") as session:
        result = session.per_suite(request)
        alone = {name: session.run(request.standalone(name))
                 for name in request.names}
    assert result.outcome is Outcome.COMPLETED, result.reason
    assert result.implementation == DEFAULT_IMPLEMENTATION
    for name, run in alone.items():
        assert result.verdicts(name) == {m.key: _verdict_of(m) for m in run.mutants}
        assert result.runs[name].identity == run.identity
    assert [v.verdict for v in result.verdicts("strong").values()] == ["killed"]
    assert [v.verdict for v in result.verdicts("weak").values()] == ["survived"]
    (row,) = result.kill_matrix()["mutants"]
    assert row["status"] == "killed"
    assert row["killers"] == list(result.verdicts("strong")[result.mutants[0].key].killers)


@pytest.mark.mutation_tool
def test_per_suite_keeps_one_operator_twice_on_a_line_apart(tmp_path):
    """Each suite kills a different one of the two additions on one line,
    and the verdicts stay with their own mutant."""
    adapter, root, _ = _project(tmp_path, (
        "test_first.py", "from arith import add3\n\n\n"
                         "def test_first():\n    assert add3(0, 1, 0) == 1\n"), (
        "test_second.py", "from arith import add3\n\n\n"
                          "def test_second():\n    assert add3(0, 0, 1) == 1\n"))
    (root / "arith.py").write_text(
        "def add3(a, b, c):\n    return a + b + c\n", encoding="utf-8")
    tests = str(root / "tests")
    request = PerSuiteRequest("arith.py", (
        BudgetedSuite("first", lb.Suite(tests, ("test_first.py",)), 10),
        BudgetedSuite("second", lb.Suite(tests, ("test_second.py",)), 10)),
        catalogue=Catalogue(operators=tuple(ADD_SUB)))
    with adapter.mutations.open_session("cosmic_ray") as session:
        result = session.per_suite(request)
    assert result.outcome is lb.Outcome.COMPLETED, result.reason
    first, second = sorted(result.mutants, key=lambda m: m.key.occurrence)
    assert first.key.line == second.key.line == 2
    assert first.key.column < second.key.column
    assert ({n: v.verdict for n, v in first.verdicts.items()}
            == {"first": "killed", "second": "survived"})
    assert ({n: v.verdict for n, v in second.verdicts.items()}
            == {"first": "survived", "second": "killed"})
