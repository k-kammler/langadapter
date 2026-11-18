"""The self-contained result of a Cosmic Ray run, read from a session: every
mutant with its key, native record, class and kill-matrix row; counts,
score and survivors; complete after the session's files are gone."""
import contextlib
import json
import shutil
import sqlite3
import sys
from dataclasses import replace

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.python._runs import run_record_mark
from langadapter.python.adapter import PythonAdapter
from langadapter.errors import InvalidRequest
from langadapter.mutation import (
    MutationClass, matrix_lower_bound_percent, matrix_score_percent, MutationRun,
)
from langadapter.python._cosmic_ray import CosmicRayEngine

#: job, line, operator, test outcome, worker outcome, output, diff
def _record(text="", **record):
    """A per-mutant output: ``text`` and its run record's line."""
    return text + "\n" + run_record_mark() + json.dumps(record)


ROWS = [
    ("j1", 3, "core/Op_A", "KILLED", "NORMAL",
     _record("FAILED tests/test_m.py::test_a - assert 1\n1 failed in 0.1s",
             failed=["tests/test_m.py::test_a"], errors=[], counts={"failed": 1}), "d1"),
    ("j2", 3, "core/Op_B", "SURVIVED", "NORMAL", _record("1 passed in 0.1s", counts={"passed": 1}), "d2"),
    ("j3", 4, "core/Op_A", "KILLED", "NORMAL", "timeout", "d3"),
    ("j4", 6, "core/Op_C", "KILLED", "NORMAL", "SyntaxError", "d4"),
    ("j5", 7, "core/Op_D", None, "SKIPPED", "Filtered operator", None),
    ("j6", 9, "core/Op_E", "KILLED", "NORMAL", "Segmentation fault", "d6"),
]


def _session(path, rows):
    # A connection's own context manager commits but does not close.
    with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE mutation_specs (job_id TEXT PRIMARY KEY, "
            "module_path TEXT, occurrence INTEGER, start_pos_col INTEGER, "
            "operator_name TEXT, start_pos_row INTEGER)")
        conn.execute(
            "CREATE TABLE work_results (job_id TEXT PRIMARY KEY, "
            "worker_outcome TEXT, output TEXT, test_outcome TEXT, diff TEXT)")
        for i, (job, line, op, to, wo, out, diff) in enumerate(rows):
            conn.execute("INSERT INTO work_items VALUES (?)", (job,))
            conn.execute(
                "INSERT INTO mutation_specs VALUES (?,?,?,?,?,?)",
                (job, "pkg/m.py", i, 4, op, line))
            if to is not None or wo is not None:
                conn.execute("INSERT INTO work_results VALUES (?,?,?,?,?)",
                             (job, wo, out, to, diff))


def _engine(tmp_path):
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    engine = CosmicRayEngine(CosmicRaySettings()._engine_config("local"), adapter=adapter)
    engine._last_requested_catalogue = "full"
    engine._last_requested_operators = ["core/Op_A"]
    return engine


def test_a_session_reads_as_a_run_of_typed_mutants(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, ROWS)
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    assert isinstance(run, MutationRun) and run.complete
    by_id = {m.engine_id: m for m in run.mutants}
    assert set(by_id) == {"j1", "j2", "j3", "j4", "j6"}   # skipped left out
    killed = by_id["j1"]
    assert (killed.status, killed.classification.mutation_class,
            killed.classification.subcategory) == (
        "killed", MutationClass.DETECTED, "failing_test")
    assert killed.killers == ("tests/test_m.py::test_a",)
    assert killed.key.file == "pkg/m.py" and killed.key.operator == "core/Op_A"
    assert (killed.key.occurrence, killed.key.line, killed.key.column) == (0, 3, 4)
    assert len(killed.key.diff_sha256) == 64
    assert (killed.test_outcome, killed.worker_outcome) == ("KILLED", "NORMAL")
    assert by_id["j3"].classification.subcategory == "timeout"
    # A mutant that breaks the tests' import comes back KILLED.
    assert by_id["j4"].classification.subcategory == "no_named_test"
    assert by_id["j6"].classification.subcategory == "no_named_test"
    assert run.counts() == {MutationClass.DETECTED: 4,
                            MutationClass.UNDETECTED: 1, MutationClass.NOT_RUN: 0}
    assert run.score() == 80.0
    assert run.progress_score() == run.score()
    assert [m.engine_id for m in run.survivors()] == ["j2"]
    assert run.survivors()[0].diff == "d2"


def test_the_run_outlives_its_session_and_serializes_as_a_matrix(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session = session_dir / "s.sqlite"
    _session(session, ROWS)
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    shutil.rmtree(session_dir)
    matrix = run.to_kill_matrix({"consumer_field": 1})
    assert list(matrix) == ["schema_version", "target_file", "mutants",
                            "summary", "catalogue_provenance",
                            "consumer_field"]
    assert matrix["summary"]["total"] == 5
    assert matrix["catalogue_provenance"]["observed_mutant_ids"] == [
        "j1", "j2", "j3", "j4", "j6"]
    # The matrix is a copy: changing it leaves the run as it was.
    matrix["mutants"][0]["status"] = "survived"
    assert run.mutants[0].status == "killed"


def test_a_run_with_untested_mutants_is_incomplete_and_has_no_score(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, ROWS + [("j7", 11, "core/Op_F", None, None, None, None)])
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    assert not run.complete and run.score() is None
    assert run.incomplete == {"reason": "untested_mutants_in_catalogue",
                              "untested": 1, "total": 6}
    # The untested mutant counts as not killed: 4 of 6, not 4 of 5.
    assert run.progress_score() == 100.0 * 4 / 6
    timed_out = replace(run, outcome=lb.Outcome.TIMED_OUT)
    assert timed_out.progress_score() == 100.0 * 4 / 6
    assert timed_out.score() is None
    matrix = run.to_kill_matrix()
    assert matrix["incomplete"]["untested"] == 1
    assert matrix_score_percent(matrix) is None
    assert matrix_lower_bound_percent(matrix) == run.progress_score()


def test_a_failed_worker_request_leaves_the_run_incomplete(tmp_path):
    """A failed request to a worker leaves a result without a test outcome,
    which measured nothing: the run is incomplete and has no score. Its
    progress score, a lower bound, counts the mutant as not killed."""
    session = tmp_path / "s.sqlite"
    _session(session, ROWS + [("j8", 12, "core/Op_G", None, "ABNORMAL",
                               "Cannot connect to host", None)])
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    assert not run.complete
    assert run.score() is None and run.progress_score() == 100.0 * 4 / 6
    assert matrix_score_percent(run.to_kill_matrix()) is None


@pytest.mark.parametrize("worker, output", [
    ("NORMAL", "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff"),
    ("EXCEPTION", "Traceback (most recent call last):\nOSError: [Errno 28]"),
])
def test_an_incompetent_result_measured_nothing(tmp_path, worker, output):
    """Cosmic Ray reports INCOMPETENT when running the tests (worker NORMAL)
    or applying the mutation (worker EXCEPTION) raised, so nothing was
    measured: the mutant is untested with that reason, not killed, and the
    run is incomplete without a score. Its progress score counts the mutant
    as not killed."""
    session = tmp_path / "s.sqlite"
    _session(session, ROWS + [("j8", 12, "core/Op_G", "INCOMPETENT", worker, output, None)])
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    (incompetent,) = [m for m in run.mutants if m.engine_id == "j8"]
    assert (incompetent.status, incompetent.classification.mutation_class,
            incompetent.classification.subcategory) == (
        "untested", MutationClass.NOT_RUN, "incompetent")
    assert run.summary["untested"] == 1 and run.summary["detected"] == 4
    assert not run.complete and run.incomplete["untested"] == 1
    assert run.score() is None and run.progress_score() == 100.0 * 4 / 6
    assert matrix_score_percent(run.to_kill_matrix()) is None


@pytest.mark.parametrize("rows,progress", [
    (ROWS[1:2], 0.0),
    ([("j7", 11, "core/Op_F", None, None, None, None)], 0.0),
    ([], None),
])
def test_progress_is_none_only_without_a_valid_mutant(tmp_path, rows, progress):
    session = tmp_path / "s.sqlite"
    _session(session, rows)
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    assert run.progress_score() == progress


def test_failed_execution_has_no_progress_score():
    run = MutationRun.failed("m.py", lb.Outcome.INFRASTRUCTURE_FAILURE, "not started")
    assert run.progress_score() is None
    assert run.score() is None and not run.complete
    assert run.failure is None


def test_a_failed_run_names_a_known_failure():
    run = MutationRun.failed("m.py", lb.Outcome.INFRASTRUCTURE_FAILURE,
                             "unreadable", failure="session_read")
    assert run.failure == "session_read" and not run.measured
    with pytest.raises(InvalidRequest) as raised:
        MutationRun.failed("m.py", lb.Outcome.INFRASTRUCTURE_FAILURE, "x",
                           failure="something_else")
    assert raised.value.field == "failure"


def test_a_crash_kill_gains_the_test_running_when_the_process_ended(
        tmp_path):
    import hashlib
    import json

    from langadapter import mutation as ev

    base = "a = 1\nb = 2\nc = 3\n"
    diff = ("--- a/m.py\n+++ b/m.py\n"
            "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 99\n c = 3\n")
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", 2, "core/Op_A", "KILLED", "NORMAL", "",
                        diff)])
    evidence = tmp_path / "evidence"
    (evidence / "local").mkdir(parents=True)
    (evidence / "baseline_module.txt").write_text(base, encoding="utf-8")
    sha = hashlib.sha1(b"a = 1\nb = 99\nc = 3\n").hexdigest()
    (evidence / "local" / "journal.jsonl").write_text("\n".join(
        json.dumps(event) for event in (
            {"run": 1, "module_sha": sha},
            {"start": "test_m.py::test_crash"})) + "\n", encoding="utf-8")
    run = _engine(tmp_path)._read_mutation_run(session, tmp_path / "m.py")
    (mutant,) = run.mutants
    assert mutant.row["active_at_end"] == "test_m.py::test_crash"
    assert mutant.status == "killed" and mutant.killers == ()
    assert mutant.evidence == (ev.CrashObservation("test_m.py::test_crash"),)
