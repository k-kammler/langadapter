"""The evidence verification and per-file adjudication record for each
mutant, on synthetic sessions with scripted re-runs: verdicts, the sweep's
record, attempts, replacement, and adjudication outcomes per file."""
import contextlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.python._runs import run_record_mark
from langadapter.python.adapter import PythonAdapter
from langadapter._linux_job import ControlError
from langadapter import mutation as ev
from langadapter.python import _cr_verify
from langadapter.python._cosmic_ray import CosmicRayEngine

def _record(text="", **record):
    """A per-mutant output: ``text`` and its run record's line."""
    return text + "\n" + run_record_mark() + json.dumps(record)


NO_REPORT = ""
REPORTED_ERROR = _record("ERROR tests/test_m.py\n==== 1 error in 0.05s ====",
                         failed=[], errors=["tests/test_m.py"], counts={"error": 1},
                         broken=["tests/test_m.py"])
ATTRIBUTED = _record("FAILED tests/test_m.py::test_a - assert 1 == 2\n1 failed in 0.1s",
                     failed=["tests/test_m.py::test_a"], errors=[], counts={"failed": 1})
PASSED = _record("1 passed in 0.1s", counts={"passed": 1})


def _session(path, rows):
    """A session: ``rows`` of (job, test outcome, worker outcome, output)."""
    with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE mutation_specs (job_id TEXT PRIMARY KEY, "
            "module_path TEXT, occurrence INTEGER, start_pos_col INTEGER, "
            "operator_name TEXT, start_pos_row INTEGER)")
        conn.execute(
            "CREATE TABLE work_results (job_id TEXT PRIMARY KEY, "
            "worker_outcome TEXT, output TEXT, test_outcome TEXT, diff TEXT)")
        for i, (job, test_outcome, worker_outcome, output) in enumerate(rows):
            conn.execute("INSERT INTO work_items VALUES (?)", (job,))
            conn.execute("INSERT INTO mutation_specs VALUES (?,?,?,?,?,?)",
                         (job, "m.py", i, 4, "core/Op", i + 1))
            conn.execute("INSERT INTO work_results VALUES (?,?,?,?,?)",
                         (job, worker_outcome, output, test_outcome, "d" + job))


def _rerun(script, calls=None):
    """A re-run stub: each job takes its next scripted (test outcome, worker
    outcome, output), or None for a run that recorded nothing."""
    def run(vdb, n, *test_file):
        with contextlib.closing(sqlite3.connect(str(vdb))) as conn, conn:
            jobs = [r[0] for r in conn.execute(
                "SELECT job_id FROM work_items ORDER BY job_id")]
            if calls is not None:
                calls.append((jobs, [f.name for f in test_file]))
            for job in jobs:
                queue = script.get((job, test_file[0].name) if test_file
                                   else job)
                result = queue.pop(0) if queue else None
                if result is not None:
                    conn.execute(
                        "INSERT INTO work_results VALUES (?,?,?,?,?)",
                        (job, result[1], result[2], result[0], "d" + job))
        return True
    return run


def _engine(tmp_path, **config):
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    engine = CosmicRayEngine({**CosmicRaySettings()._engine_config("local"), **config}, adapter=adapter)
    engine._last_requested_catalogue = "full"
    engine._last_requested_operators = []
    return engine


def _verify(engine, session, rerun, sess=None):
    engine._verify_unattributed_kills(
        session_path=session,
        sess=sess or SimpleNamespace(verified_killed_by_all=set()),
        exec_verify=rerun, suite="suite-a")


def _rows(engine, session, tmp_path):
    run = engine._read_mutation_run(session, tmp_path / "m.py")
    return {m.engine_id: m for m in run.mutants}


def test_each_verdict_is_recorded_with_its_attempts(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [
        ("corrected", "KILLED", "NORMAL", NO_REPORT),
        ("recovered", "KILLED", "NORMAL", NO_REPORT),
        ("hang", "KILLED", "NORMAL", "timeout"),
        ("broken", "KILLED", "NORMAL", NO_REPORT),
        ("dies", "KILLED", "ABNORMAL", NO_REPORT),
        ("attributed", "KILLED", "NORMAL", ATTRIBUTED),
    ])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({
        "corrected": [("SURVIVED", "NORMAL", PASSED)],
        "recovered": [("KILLED", "NORMAL", ATTRIBUTED)],
        "hang": [("KILLED", "NORMAL", "timeout")],
        "broken": [("KILLED", "NORMAL", REPORTED_ERROR)],
        "dies": [None, ("KILLED", "ABNORMAL", NO_REPORT)],
    }))
    mutants = _rows(engine, session, tmp_path)
    assert "verification" not in mutants["attributed"].row
    evidence = {job: m.evidence[0] for job, m in mutants.items()
                if job != "attributed"}
    assert {job: e.verdict for job, e in evidence.items()} == {
        "corrected": ev.CORRECTED, "recovered": ev.RECOVERED,
        "hang": ev.REPRODUCED_TIMEOUT, "broken": ev.REPRODUCED_WITH_REPORT,
        "dies": ev.UNRESOLVED}
    assert {job for job, e in evidence.items() if e.replaced} == {
        "corrected", "recovered"}
    assert {job for job, e in evidence.items() if e.clock} == {"hang"}
    corrected = evidence["corrected"]
    assert corrected.recorded == ev.NativeRecord("KILLED", "NORMAL")
    assert corrected.attempts == (
        ev.NativeRecord("SURVIVED", "NORMAL", reported=True),)
    # The fresh record replaced the sweep's: the row is the survival.
    assert mutants["corrected"].status == "survived"
    assert evidence["recovered"].attempts[0].killers == (
        "tests/test_m.py::test_a",)
    assert mutants["recovered"].killers == ("tests/test_m.py::test_a",)
    assert evidence["broken"].attempts == (
        ev.NativeRecord("KILLED", "NORMAL", reported=True),)
    assert evidence["dies"].attempts == (
        None, ev.NativeRecord("KILLED", "ABNORMAL"))
    assert mutants["dies"].row["unresolved"] is True
    assert mutants["hang"].status == "timeout"


def test_a_kill_verified_earlier_in_the_session_is_not_run_again(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("broken", "KILLED", "NORMAL", NO_REPORT)])
    engine, sess = _engine(tmp_path), SimpleNamespace(
        verified_killed_by_all=set())
    _verify(engine, session,
            _rerun({"broken": [("KILLED", "NORMAL", REPORTED_ERROR)]}), sess)
    calls = []
    _verify(engine, session, _rerun({}, calls), sess)
    assert calls == []
    (verification,) = _rows(engine, session, tmp_path)["broken"].evidence
    assert (verification.verdict, verification.attempts,
            verification.replaced) == (ev.CACHED_GENUINE, (), False)


def test_a_verification_that_cannot_run_keeps_the_record(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, lambda vdb, n: False)
    mutant = _rows(engine, session, tmp_path)["crash"]
    assert mutant.evidence == (ev.Verification(
        ev.NOT_VERIFIED, False, ev.NativeRecord("KILLED", "NORMAL")),)
    assert "unresolved" not in mutant.row


def test_suspects_after_a_batch_without_evidence_are_not_attempted(
        tmp_path, monkeypatch):
    monkeypatch.setattr(_cr_verify, "_KILL_VERIFY_MAX", 1)
    session = tmp_path / "s.sqlite"
    _session(session, [("a", "KILLED", "NORMAL", NO_REPORT),
                       ("b", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    mutants = _rows(engine, session, tmp_path)
    assert mutants["a"].evidence[0].verdict == ev.UNRESOLVED
    assert mutants["a"].evidence[0].attempts == (None, None)
    assert mutants["b"].evidence[0].verdict == ev.NOT_ATTEMPTED
    assert mutants["b"].evidence[0].attempts == ()
    assert mutants["b"].row["unresolved"] is True


def test_a_replacement_the_session_did_not_take_is_not_replaced(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("corrected", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    rerun = _rerun({"corrected": [("SURVIVED", "NORMAL", PASSED)]})

    def rerun_then_lock_the_session(vdb, n):
        done = rerun(vdb, n)
        os.chmod(session, stat.S_IREAD)
        return done

    try:
        _verify(engine, session, rerun_then_lock_the_session)
    finally:
        os.chmod(session, stat.S_IREAD | stat.S_IWRITE)
    (verification,) = _rows(engine, session, tmp_path)["corrected"].evidence
    assert (verification.verdict, verification.replaced) == (
        ev.CORRECTED, False)
    assert _rows(engine, session, tmp_path)["corrected"].status == "killed"


def test_outputs_are_evidence_when_the_run_keeps_them(tmp_path):
    session = tmp_path / "s.sqlite"
    crash = "Fatal Python error: Segmentation fault\n" + "x" * 6000
    _session(session, [("crash", "KILLED", "NORMAL", crash)])
    engine = _engine(tmp_path, cosmic_ray_keep_output=True)
    _verify(engine, session, _rerun({"crash": [
        ("SURVIVED", "NORMAL", PASSED)]}))
    (verification,) = _rows(engine, session, tmp_path)["crash"].evidence
    assert verification.recorded.output.startswith("Fatal Python error")
    assert "...[truncated]..." in verification.recorded.output
    # A kept output is pytest's, without the run record's line.
    assert verification.attempts[0].output == "1 passed in 0.1s"


def _adjudicate(engine, session, rerun, files, sess=None):
    engine._adjudicate_unresolved_per_file(
        session_path=session, exec_adjudicate=rerun,
        test_files=[Path("tests") / name for name in files], sess=sess)


def _record(session, job):
    with contextlib.closing(sqlite3.connect(str(session))) as conn:
        return conn.execute("SELECT test_outcome, output FROM work_results "
                            "WHERE job_id = ?", (job,)).fetchone()


def test_each_adjudication_outcome_names_every_files_verdict(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("alone", "KILLED", "NORMAL", NO_REPORT),
                       ("together", "KILLED", "NORMAL", NO_REPORT),
                       ("partial", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    assert engine._last_unresolved_job_ids == {"alone", "together", "partial"}
    _adjudicate(engine, session, _rerun({
        ("alone", "test_a.py"): [("KILLED", "NORMAL", NO_REPORT)],
        ("alone", "test_b.py"): [("SURVIVED", "NORMAL", PASSED)],
        ("together", "test_a.py"): [("SURVIVED", "NORMAL", PASSED)],
        ("together", "test_b.py"): [("SURVIVED", "NORMAL", PASSED)],
        ("partial", "test_a.py"): [("KILLED", "NORMAL", NO_REPORT)],
    }), ["test_a.py", "test_b.py"])
    mutants = _rows(engine, session, tmp_path)
    alone = mutants["alone"]
    assert [e.kind for e in alone.evidence] == [
        "verification", "file_attribution"]
    verification, attribution = alone.evidence
    assert verification.verdict == ev.UNRESOLVED
    assert attribution == ev.FileAttribution(
        ev.ATTRIBUTED, (("test_a.py", True), ("test_b.py", False)))
    assert attribution.files == ("test_a.py",)
    assert alone.killers == ("test_a.py::per_file_adjudication",)
    assert "unresolved" not in alone.row
    together = mutants["together"].evidence[1]
    assert together == ev.FileAttribution(
        ev.POOL_CONTEXT, (("test_a.py", False), ("test_b.py", False)))
    assert mutants["together"].row["unresolved"] is True
    partial = mutants["partial"].evidence[1]
    assert partial == ev.FileAttribution(
        ev.ATTRIBUTION_INCOMPLETE, (("test_a.py", True), ("test_b.py", None)))
    assert mutants["partial"].row["unresolved"] is True


def test_a_single_test_file_is_not_adjudicated(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    _adjudicate(engine, session, _rerun({}), ["test_a.py"])
    kinds = [e.kind for e in _rows(engine, session, tmp_path)["crash"].evidence]
    assert kinds == ["verification"]


def test_a_new_run_starts_without_the_last_runs_evidence(tmp_path):
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    _adjudicate(engine, session, _rerun({}), ["test_a.py", "test_b.py"])
    assert engine._last_verification and engine._last_adjudication
    _verify(engine, session, _rerun({"crash": [
        ("SURVIVED", "NORMAL", PASSED)]}))
    _adjudicate(engine, session, _rerun({}), ["test_a.py", "test_b.py"])
    assert engine._last_adjudication == {}
    kinds = [e.kind for e in _rows(engine, session, tmp_path)["crash"].evidence]
    assert kinds == ["verification"]


class _Command:
    """The command a failed run carries, disposed or not."""

    def __init__(self, disposed):
        self._langadapter_command_disposed = disposed


@pytest.mark.parametrize("disposed", [False, True])
def test_adjudication_stops_after_a_run_leaves_its_command_unresolved(tmp_path, disposed):
    """A command not disposed may still write the module under test, so no
    further run starts and the error ends the run, records unchanged. A
    disposed command only costs its file the verdict."""
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    failure = ControlError("Supervisor heartbeat did not stop")
    failure._langadapter_command = _Command(disposed)
    rerun, files = _rerun({("crash", "test_b.py"): [("KILLED", "NORMAL", NO_REPORT)],
                           ("crash", "test_c.py"): [("SURVIVED", "NORMAL", PASSED)]}), []

    def adjudicate(vdb, n, test_file):
        files.append(test_file.name)
        if len(files) == 1:
            raise failure
        return rerun(vdb, n, test_file)

    names = ["test_a.py", "test_b.py", "test_c.py"]
    if disposed:
        _adjudicate(engine, session, adjudicate, names)
        assert files == names
        assert engine._last_adjudication["crash"] == ev.FileAttribution(
            ev.ATTRIBUTION_INCOMPLETE, (("test_a.py", None), ("test_b.py", True), ("test_c.py", False)))
    else:
        with pytest.raises(ControlError) as caught:
            _adjudicate(engine, session, adjudicate, names)
        assert caught.value is failure and files == ["test_a.py"]
    assert _record(session, "crash") == ("KILLED", NO_REPORT)
    assert engine._last_unresolved_job_ids == {"crash"}


@pytest.mark.parametrize("state", ["command", "restore"])
def test_adjudication_stops_when_the_session_holds_the_module_unresolved(tmp_path, state):
    """When the session keeps an unresolved command or could not restore the
    module under test, adjudication re-raises and starts no further file."""
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    _verify(engine, session, _rerun({}))
    sess = SimpleNamespace(_target_commands=[], _source_restore_failed=False)
    failure, files = OSError("auxiliary writer is still uncertain"), []

    def adjudicate(vdb, n, test_file):
        files.append(test_file.name)
        if state == "command":
            sess._target_commands.append(_Command(False))
        else:
            sess._source_restore_failed = True
        raise failure

    with pytest.raises(OSError) as caught:
        _adjudicate(engine, session, adjudicate, ["test_a.py", "test_b.py"], sess)
    assert caught.value is failure and files == ["test_a.py"]
    assert _record(session, "crash") == ("KILLED", NO_REPORT)


@pytest.mark.parametrize("raised", ["control", "os", "timeout"])
def test_verification_ends_the_run_when_a_run_leaves_the_module_unresolved(tmp_path, raised):
    """A re-run that leaves the module unresolved ends the verification with
    its error after the first run, the record unchanged."""
    session = tmp_path / "s.sqlite"
    _session(session, [("crash", "KILLED", "NORMAL", NO_REPORT)])
    engine = _engine(tmp_path)
    sess = SimpleNamespace(verified_killed_by_all=set(), _target_commands=[],
                           _source_restore_failed=False)
    failure = {"control": ControlError("lease expired"),
               "os": OSError("auxiliary writer is still uncertain"),
               "timeout": subprocess.TimeoutExpired("verify", 1)}[raised]
    runs = []

    def rerun(vdb, n):
        runs.append(n)
        sess._target_commands.append(_Command(False))
        raise failure

    with pytest.raises(type(failure)) as caught:
        _verify(engine, session, rerun, sess)
    assert caught.value is failure and runs == [1]
    assert _record(session, "crash") == ("KILLED", NO_REPORT)
