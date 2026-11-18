"""Unattributed-kill verification: with scripted verification results, the
updates to the session, cache, unresolved set and captured matrix; and one
pass with real Cosmic Ray."""
from __future__ import annotations

import re
import json
import contextlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter import mutation as _kill_matrix
from langadapter.python._runs import run_record_mark
from langadapter.python.adapter import PythonAdapter
from langadapter.python._cosmic_ray import CosmicRayEngine as _Engine
from langadapter.python._cr_verify import (
    MutationInstrumentFault, _pytest_reported,
)
import subprocess
from langadapter.python import _cosmic_ray as engine_module, _cr_verify


def _record(text="", **record):
    """A per-mutant output: ``text`` and its run record's line."""
    return text + "\n" + run_record_mark() + json.dumps(record)


def _local_engine(config):
    """An engine for a local project in the calling interpreter."""
    context = lb.ProjectContext(
        root=str(Path(__file__).parent),
        interpreter=lb.Interpreter(path=sys.executable))
    return _Engine({**CosmicRaySettings()._engine_config("local"), **config}, adapter=PythonAdapter(context))


class _RetryEngine(_Engine):
    """An engine with the settings of an out-of-memory retry: an unresolved
    kill fails the run and auxiliary passes run serially."""

    fail_on_unresolved = True
    serial_auxiliary = True

ATTRIBUTED = _record("FAILED tests/test_mod.py::test_a - assert 1 == 2\n1 failed in 0.1s",
                     failed=["tests/test_mod.py::test_a"], errors=[], counts={"failed": 1})
REPORTED_KILLED_BY_ALL = _record(
    "ERROR tests/test_mod.py\n==== 1 error in 0.05s ====",
    failed=[], errors=["tests/test_mod.py"], counts={"error": 1}, broken=["tests/test_mod.py"])
REPORTED_COLLECTION_ERRORS = (
    "!!!!!!!!!!!!!!!!!!! Interrupted: 39 errors during collection "
    "!!!!!!!!!!!!!!!!!!!\n7 warnings, 39 errors in 2.45s"
)
REPORTED_COLLECTION_ERRORS = _record(
    REPORTED_COLLECTION_ERRORS, failed=[], errors=[], counts={"warnings": 7, "error": 39})


def _make_session(path: Path, rows) -> None:
    """A minimal Cosmic Ray session db. A row with ``test_outcome=None``
    is pending: a work item without a result."""
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
            "operator_name TEXT, occurrence INTEGER, start_pos_row INTEGER, "
            "start_pos_col INTEGER)")
        conn.execute(
            "CREATE TABLE work_results (job_id TEXT PRIMARY KEY, "
            "worker_outcome TEXT, output TEXT, test_outcome TEXT, diff TEXT)")
        for r in rows:
            conn.execute("INSERT INTO work_items VALUES (?)", (r["job_id"],))
            conn.execute(
                "INSERT INTO mutation_specs VALUES (?, ?, ?, ?, ?, ?)",
                (r["job_id"], r.get("module", "m.py"),
                 r.get("operator", "core/ReplaceTrueWithFalse"),
                 r.get("occurrence", 0), r.get("line", 7), r.get("column")))
            if r.get("test_outcome") is not None:
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    (r["job_id"], r.get("worker_outcome", "NORMAL"),
                     r.get("output", ""), r["test_outcome"], r.get("diff", "")))
        conn.commit()


def _row(db: Path, job_id: str):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT worker_outcome, output, test_outcome FROM work_results "
            "WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        conn.close()


def _scripted_exec(script, calls):
    """An ``exec_verify`` stub that records the next scripted result of
    ``script[job_id]`` for each job (None stands for a process death)."""
    def _exec(vdb: Path, n: int) -> bool:
        with contextlib.closing(sqlite3.connect(str(vdb))) as conn:
            jobs = [r[0] for r in conn.execute(
                "SELECT job_id FROM work_items ORDER BY job_id")]
            calls.append(jobs)
            for job_id in jobs:
                outcomes = script.get(job_id)
                spec = outcomes.pop(0) if outcomes else None
                if spec is None:
                    continue
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    (job_id, spec.get("worker_outcome", "NORMAL"),
                     spec.get("output", ""), spec["test_outcome"],
                     spec.get("diff", "")))
            conn.commit()
        return True
    return _exec


def _engine(logs=None, *, retry=False):
    eng = _RetryEngine({}) if retry else _local_engine({})
    log = (lambda m, level=0: logs.append(m)) if logs is not None else None
    return eng, log


def _sess():
    return SimpleNamespace(verified_killed_by_all=set())


def _run_pass(
    eng, db, sess, exec_verify, log=None, suite="suite-a",
):
    eng._verify_unattributed_kills(
        session_path=db,
        sess=sess,
        exec_verify=exec_verify,
        suite=suite,
        log_fn=log,
    )


_JOURNAL_BASE = "a = 1\nb = 2\nc = 3\n"
_JOURNAL_DIFF = (
    "--- a/m.py\n+++ b/m.py\n"
    "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 99\n c = 3\n"
)


def _write_exact_journal(root: Path, blocks) -> Path:
    import hashlib
    from langadapter.python import _mutation as TE

    mutated = TE.apply_unified_diff(_JOURNAL_BASE, _JOURNAL_DIFF)
    assert mutated is not None
    sha = hashlib.sha1(mutated.encode("utf-8")).hexdigest()
    evidence = root / "evidence"
    worker = evidence / "local"
    worker.mkdir(parents=True, exist_ok=True)
    (evidence / "baseline_module.txt").write_text(
        _JOURNAL_BASE, encoding="utf-8",
    )
    events = []
    for run, body in enumerate(blocks, start=1):
        events.append({"run": run, "module_sha": sha})
        events.extend(body)
    (worker / "journal.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return worker / "journal.jsonl"


@pytest.mark.parametrize("exit_status", [0, 1, 2, 5])
def test_consolidation_retry_does_not_substitute_binary_journal_verdicts(
    tmp_path: Path, exit_status,
) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [{
        "job_id": "b", "test_outcome": "KILLED", "output": "",
        "worker_outcome": "ABNORMAL", "diff": _JOURNAL_DIFF,
    }])
    journal = _write_exact_journal(tmp_path, [[{"end": 1, "exit": exit_status}]])
    original_journal = journal.read_bytes()
    original_row = _row(db, "b")
    calls = []
    eng, _ = _engine(retry=True)
    session = _sess()

    _run_pass(eng, db, session, _scripted_exec({}, calls))

    assert calls == [["b"], ["b"]]
    assert _row(db, "b") == original_row
    assert journal.read_bytes() == original_journal
    assert eng._last_unresolved_job_ids == {"b"}
    assert session.verified_killed_by_all == set()


def test_suspects_are_only_the_unattributed_kills(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
        {"job_id": "c", "test_outcome": "SURVIVED"},
        {"job_id": "d", "test_outcome": "KILLED", "output": "timeout"},
        {"job_id": "e", "test_outcome": "KILLED",
         "worker_outcome": "SKIPPED"},
        {"job_id": "f", "test_outcome": None},
    ])
    eng, _ = _engine()
    suspects = eng._query_unattributed_kills(db)
    assert [s["job_id"] for s in suspects] == ["b", "d"]


def test_kill_verification_reports_its_progress_with_rising_counts(tmp_path: Path) -> None:
    """The pass reports "kill verifications" progress: zero at the start,
    rising counts during a batch and the checked count at the end."""
    import time

    from langadapter.mutation import MutationProgress

    db = tmp_path / "s.sqlite"
    _make_session(db, [{"job_id": job, "test_outcome": "KILLED", "output": ""}
                       for job in ("a", "b", "c")])
    eng, _ = _engine()
    events = []
    eng.config["_progress"] = events.append

    def exec_one_per_second(vdb: Path, n: int) -> bool:
        with contextlib.closing(sqlite3.connect(str(vdb))) as conn:
            jobs = [r[0] for r in conn.execute("SELECT job_id FROM work_items ORDER BY job_id")]
            for job in jobs:
                conn.execute("INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                             (job, "NORMAL", ATTRIBUTED, "KILLED", ""))
                conn.commit()
                time.sleep(1.2)
        return True

    _run_pass(eng, db, _sess(), exec_one_per_second)
    assert events and all(isinstance(e, MutationProgress) for e in events)
    assert {e.kind for e in events} == {"kill verifications"}
    assert {e.total for e in events} == {3}
    finished = [e.finished for e in events]
    assert finished[0] == 0 and finished[-1] == 3
    assert finished == sorted(finished) and set(finished) & {1, 2}, finished


def test_normal_verification_log_is_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [{
        "job_id": "b", "test_outcome": "KILLED", "output": "",
    }])
    eng, log = _engine(logs := [])
    _run_pass(
        eng,
        db,
        _sess(),
        _scripted_exec({"b": [{"test_outcome": "SURVIVED"}]}, []),
        log,
    )

    assert logs[0] == (
        "[kill-verify] 1 unattributed kill(s) to verify "
        "(0 clock verdict(s), 0 cached genuine)"
    )


def test_clock_verdicts_are_suspects(tmp_path: Path) -> None:
    """Kills with the output ``timeout`` are suspects flagged ``clock``,
    attributed kills and skipped rows are not."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
        {"job_id": "e", "test_outcome": "KILLED",
         "worker_outcome": "SKIPPED"},
        {"job_id": "t", "test_outcome": "KILLED", "output": "timeout"},
    ])
    eng, _ = _engine()
    suspects = eng._query_unattributed_kills(db)
    got = {s["job_id"]: s.get("clock") for s in suspects}
    assert got == {"b": False, "t": True}


def test_clock_kill_upgraded_when_rerun_names_killers(tmp_path: Path) -> None:
    """A clock kill whose re-run fails with named killers was a slowdown,
    not a hang: the fresh row replaces it."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "t", "test_outcome": "KILLED", "output": "timeout"},
    ])
    eng, log = _engine(logs := [])
    exec_verify = _scripted_exec(
        {"t": [{"test_outcome": "KILLED", "output": ATTRIBUTED}]}, [])
    _run_pass(eng, db, _sess(), exec_verify, log)
    worker, output, outcome = _row(db, "t")
    assert outcome == "KILLED" and output == ATTRIBUTED
    assert any("(clock)" in m and "attribution recovered" in m for m in logs)


def test_clock_kill_reproduced_hang_kept_and_cached(tmp_path: Path) -> None:
    """A clock kill whose re-run times out again is a genuine hang: the row
    stays and its identity is cached for later sweeps."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "t", "test_outcome": "KILLED", "output": "timeout"},
    ])
    eng, log = _engine(logs := [])
    sess = _sess()
    exec_verify = _scripted_exec(
        {"t": [{"test_outcome": "KILLED", "output": "timeout"}]}, [])
    _run_pass(eng, db, sess, exec_verify, log)
    worker, output, outcome = _row(db, "t")
    assert outcome == "KILLED" and output == "timeout"
    assert sess.verified_killed_by_all
    assert not eng._last_unresolved_job_ids


def test_clock_kill_survived_rerun_is_replaced(tmp_path: Path) -> None:
    """A clock kill whose re-run survives was transient: the survival
    replaces it."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "t", "test_outcome": "KILLED", "output": "timeout"},
    ])
    eng, log = _engine(logs := [])
    exec_verify = _scripted_exec(
        {"t": [{"test_outcome": "SURVIVED", "output": ""}]}, [])
    _run_pass(eng, db, _sess(), exec_verify, log)
    worker, output, outcome = _row(db, "t")
    assert outcome == "SURVIVED"


def test_instrument_fault_zero_collected_kill_aborts(tmp_path: Path) -> None:
    """A KILLED row whose run collected zero tests is a broken worker
    environment: the run aborts with the named fault."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "z", "test_outcome": "KILLED",
         "output": _record("no tests ran in 0.12s", counts={})},
    ])
    eng, _ = _engine()
    with pytest.raises(MutationInstrumentFault, match="zero"):
        eng._raise_on_instrument_fault(db)


def test_instrument_fault_mass_unresolved_aborts(tmp_path: Path) -> None:
    """Ten or more re-run suspects that all die again without reporting
    abort the run."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
    ])
    eng, _ = _engine()
    eng._last_verify_checked = 10
    eng._last_unresolved_job_ids = {f"j{i}" for i in range(10)}
    with pytest.raises(MutationInstrumentFault, match="no.*evidence"):
        eng._raise_on_instrument_fault(db)


def test_instrument_fault_clean_session_passes(tmp_path: Path) -> None:
    """Attributed kills and a small unresolved remainder are not a fault."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "c", "test_outcome": "SURVIVED"},
    ])
    eng, _ = _engine()
    eng._last_verify_checked = 3
    eng._last_unresolved_job_ids = {"j1"}
    eng._raise_on_instrument_fault(db)


def test_retry_instrument_fault_rejects_one_unresolved_kill(
    tmp_path: Path,
) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
    ])
    eng, _ = _engine()
    eng._last_verify_checked = 3
    eng._last_unresolved_job_ids = {"j1"}

    with pytest.raises(MutationInstrumentFault,
                       match="unresolved kills fail this run"):
        eng._raise_on_instrument_fault(
            db, reject_any_unresolved=True,
        )


def test_retry_instrument_fault_rejects_verifier_failure(
    tmp_path: Path,
) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
    ])
    eng, _ = _engine()
    eng._last_verify_exec_failed = True

    with pytest.raises(MutationInstrumentFault, match="executor failed"):
        eng._raise_on_instrument_fault(
            db, reject_any_unresolved=True,
        )


def test_spec_less_kill_has_none_identity_and_is_never_cached(
    tmp_path: Path,
) -> None:
    """A KILLED row without a mutation_specs parent gets a None identity:
    it is verified, but its genuine reproduction is never cached, so other
    spec-less suspects cannot share the key and skip verification."""
    db = tmp_path / "s.sqlite"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE mutation_specs (job_id TEXT, module_path TEXT, "
            "operator_name TEXT, occurrence INTEGER, start_pos_row INTEGER, "
            "start_pos_col INTEGER)")
        conn.execute(
            "CREATE TABLE work_results (job_id TEXT PRIMARY KEY, "
            "worker_outcome TEXT, output TEXT, test_outcome TEXT, diff TEXT)")
        conn.execute("INSERT INTO work_items VALUES ('b')")
        conn.execute(
            "INSERT INTO work_results VALUES ('b', 'NORMAL', '', 'KILLED', '')")
        conn.commit()

    eng, _ = _engine()
    suspects = eng._query_unattributed_kills(db)
    assert [s["job_id"] for s in suspects] == ["b"]
    assert suspects[0]["identity"] is None

    sess = _sess()
    _run_pass(eng, db, sess,
              _scripted_exec({"b": [{"test_outcome": "KILLED",
                                     "output": REPORTED_KILLED_BY_ALL}]}, []))
    assert _row(db, "b")[2] == "KILLED"          # kept as genuine
    assert sess.verified_killed_by_all == set()  # the None key is not cached


def test_transient_kill_replaced_by_fresh_measurement(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    _run_pass(eng, db, _sess(),
              _scripted_exec({"b": [{"test_outcome": "SURVIVED",
                                     "output": _record("1 passed in 0.1s", counts={"passed": 1})}]}, calls),
              log)
    assert _row(db, "b")[2] == "SURVIVED"
    assert len(calls) == 1
    assert any("transient" in m and "replaced" in m for m in logs)
    assert eng._last_unresolved_job_ids == set()


def test_recovered_attribution_replaces_the_record(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    _run_pass(eng, db, _sess(),
              _scripted_exec({"b": [{"test_outcome": "KILLED",
                                     "output": ATTRIBUTED}]}, calls), log)
    w, out, t = _row(db, "b")
    assert t == "KILLED" and "test_a" in out
    assert any("attribution recovered" in m for m in logs)


def test_genuine_killed_by_all_kept_and_cached_across_job_ids(
    tmp_path: Path,
) -> None:
    """A reproduced kill with a pytest report is genuine: the row stands and
    the mutant identity is cached, so the next sweep's job for the same
    mutant is not verified again."""
    sess = _sess()
    logs, calls = [], []
    eng, log = _engine(logs)

    db1 = tmp_path / "s1.sqlite"
    _make_session(db1, [
        {"job_id": "sweep1-id", "test_outcome": "KILLED", "output": "",
         "operator": "core/NumberReplacer", "occurrence": 3},
    ])
    _run_pass(eng, db1, sess,
              _scripted_exec({"sweep1-id": [
                  {"test_outcome": "KILLED",
                   "output": REPORTED_KILLED_BY_ALL}]}, calls), log)
    assert _row(db1, "sweep1-id")[2] == "KILLED"      # kept
    assert ("suite-a", ("m.py", "core/NumberReplacer", 3)) in sess.verified_killed_by_all
    assert len(calls) == 1

    db2 = tmp_path / "s2.sqlite"
    _make_session(db2, [
        {"job_id": "sweep2-id", "test_outcome": "KILLED", "output": "",
         "operator": "core/NumberReplacer", "occurrence": 3},
    ])
    _run_pass(eng, db2, sess, _scripted_exec({}, calls), log)
    assert len(calls) == 1                            # cache hit: no exec
    assert any("1 cached genuine" in m for m in logs)


def test_a_genuine_kill_is_cached_for_its_suite_only(tmp_path: Path) -> None:
    """A genuine kill is cached per suite fingerprint: another suite is
    verified again, and a suite without a fingerprint caches nothing."""
    sess = _sess()
    calls = []
    eng, log = _engine([])
    for index, suite in enumerate(("suite-a", "suite-b", None, None)):
        db = tmp_path / f"s{index}.sqlite"
        _make_session(db, [
            {"job_id": f"sweep{index}", "test_outcome": "KILLED", "output": "",
             "operator": "core/NumberReplacer", "occurrence": 3},
        ])
        reproduced = [{"test_outcome": "KILLED", "output": REPORTED_KILLED_BY_ALL}]
        _run_pass(eng, db, sess, _scripted_exec({f"sweep{index}": reproduced}, calls),
                  log, suite=suite)
    assert len(calls) == 4
    assert sess.verified_killed_by_all == {
        ("suite-a", ("m.py", "core/NumberReplacer", 3)),
        ("suite-b", ("m.py", "core/NumberReplacer", 3))}


def test_a_suites_fingerprint_follows_its_files_and_command(tmp_path: Path) -> None:
    from langadapter.python._cosmic_ray import _suite_fingerprint

    suite = tmp_path / "tests"
    (suite / "__pycache__").mkdir(parents=True)
    (suite / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    first = _suite_fingerprint(suite, "pytest -x tests")
    (suite / "__pycache__" / "test_a.pyc").write_bytes(b"\0")
    assert _suite_fingerprint(suite, "pytest -x tests") == first
    assert _suite_fingerprint(suite, "pytest -x tests/test_a.py") != first
    (suite / "test_a.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
    assert _suite_fingerprint(suite, "pytest -x tests") != first


def test_normal_verification_rejects_abnormal_collection_report(
    tmp_path: Path,
) -> None:
    """Outside the retry, an abnormal collection report stays unresolved."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    eng, _ = _engine()
    _run_pass(
        eng,
        db,
        _sess(),
        _scripted_exec({"b": [
            {
                "test_outcome": "KILLED",
                "worker_outcome": "ABNORMAL",
                "output": REPORTED_COLLECTION_ERRORS,
            },
            {
                "test_outcome": "KILLED",
                "worker_outcome": "ABNORMAL",
                "output": REPORTED_COLLECTION_ERRORS,
            },
        ]}, []),
    )

    assert eng._last_unresolved_job_ids == {"b"}


def test_no_evidence_gets_one_reattempt_then_unresolved(
    tmp_path: Path,
) -> None:
    """A kill reproduced without measurement evidence is re-attempted once,
    then the row is kept, nothing cached and the mutant unresolved."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    sess = _sess()
    logs, calls = [], []
    eng, log = _engine(logs)
    _run_pass(eng, db, sess,
              _scripted_exec({"b": [
                  {"test_outcome": "KILLED", "output": "",
                   "worker_outcome": "ABNORMAL"},
                  {"test_outcome": "KILLED", "output": ""},
              ]}, calls), log)
    assert len(calls) == 2                            # the re-attempt bound
    assert _row(db, "b")[2] == "KILLED"               # kept unchanged
    assert sess.verified_killed_by_all == set()       # never cached
    assert eng._last_unresolved_job_ids == {"b"}
    assert any("kept unresolved" in m for m in logs)


def test_verification_timeout_is_terminal_and_cached(tmp_path: Path) -> None:
    """A verification timeout reproduces a hang: the row is kept and cached
    so later sweeps skip it."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    sess = _sess()
    logs, calls = [], []
    eng, log = _engine(logs)
    _run_pass(eng, db, sess,
              _scripted_exec({"b": [{"test_outcome": "KILLED",
                                     "output": "timeout"}]}, calls), log)
    assert len(calls) == 1
    assert _row(db, "b")[2] == "KILLED"               # kept
    assert ("suite-a", ("m.py", "core/ReplaceTrueWithFalse", 0)) in (
        sess.verified_killed_by_all)
    assert eng._last_unresolved_job_ids == set()
    assert any("clock verdict" in m for m in logs)


def test_null_outcome_verification_row_is_no_evidence(tmp_path: Path) -> None:
    """A verification row without a test outcome (dead worker or failed
    dispatch) does not replace the record, and after the re-attempt the
    mutant is unresolved."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": "",
         "diff": "--- original diff"},
    ])
    sess = _sess()
    calls = []
    eng, _ = _engine()
    dead = {"test_outcome": None, "worker_outcome": "ABNORMAL",
            "output": "HTTP request to worker failed"}
    _run_pass(eng, db, sess,
              _scripted_exec({"b": [dead, dict(dead)]}, calls))
    assert len(calls) == 2
    w, out, t = _row(db, "b")
    assert t == "KILLED" and w == "NORMAL"            # original untouched
    assert sess.verified_killed_by_all == set()
    assert eng._last_unresolved_job_ids == {"b"}


def test_later_exec_failure_keeps_earlier_corrections(tmp_path: Path) -> None:
    """When attempt 2 fails to execute, attempt 1's measurement of A still
    lands in the session and B, without evidence, stays unmarked."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": "",
         "occurrence": 1},
        {"job_id": "b", "test_outcome": "KILLED", "output": "",
         "occurrence": 2},
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    inner = _scripted_exec({
        "a": [{"test_outcome": "SURVIVED", "output": _record("2 passed in 0.1s", counts={"passed": 2})}],
        "b": [None],
    }, calls)

    def flaky_exec(vdb: Path, n: int) -> bool:
        if len(calls) >= 1:
            calls.append("failed")
            return False
        return inner(vdb, n)

    _run_pass(eng, db, _sess(), flaky_exec, log)
    assert _row(db, "a")[2] == "SURVIVED"             # correction applied
    assert _row(db, "b")[2] == "KILLED"               # kept unchanged
    assert eng._last_unresolved_job_ids == set()      # not marked: no evidence
    assert any("unverified records kept unchanged" in m for m in logs)
    assert any("1 corrected" in m for m in logs)


def test_attempt2_db_raise_keeps_earlier_corrections(tmp_path: Path) -> None:
    """A db operation that raises on the second attempt is handled like an
    exec failure: attempt 1's correction is kept and the unverified row
    stays unmarked."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": "",
         "occurrence": 1},
        {"job_id": "b", "test_outcome": "KILLED", "output": "",
         "occurrence": 2},
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    inner = _scripted_exec({
        "a": [{"test_outcome": "SURVIVED", "output": _record("2 passed in 0.1s", counts={"passed": 2})}],
        "b": [None],
    }, calls)

    def raising_exec(vdb: Path, n: int) -> bool:
        if len(calls) >= 1:                 # second attempt
            raise OSError("No space left on device")
        return inner(vdb, n)

    _run_pass(eng, db, _sess(), raising_exec, log)
    assert _row(db, "a")[2] == "SURVIVED"             # correction applied
    assert _row(db, "b")[2] == "KILLED"               # kept unchanged
    assert eng._last_unresolved_job_ids == set()      # not marked: no evidence
    # Testuary shows a log line on the console when, after its bracketed
    # prefixes, it starts with WARNING (or ERROR, WARN, FATAL, CRITICAL).
    assert any(re.match(r"\s*(?:\[[^\]]*\]\s*)*WARNING\b", m) and "db operation failed" in m
               for m in logs)
    assert any("1 corrected" in m for m in logs)


def test_attempt2_lease_expiry_keeps_earlier_corrections(tmp_path: Path) -> None:
    """An expired supervisor lease raises ControlError and the pass keeps
    attempt 1's correction."""
    from langadapter._linux_job import ControlError

    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": "",
         "occurrence": 1},
        {"job_id": "b", "test_outcome": "KILLED", "output": "",
         "occurrence": 2},
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    inner = _scripted_exec({
        "a": [{"test_outcome": "SURVIVED", "output": _record("2 passed in 0.1s", counts={"passed": 2})}],
        "b": [None],
    }, calls)

    def expiring_exec(vdb: Path, n: int) -> bool:
        if len(calls) >= 1:
            raise ControlError(
                "Native execution did not complete: host control lease expired")
        return inner(vdb, n)

    _run_pass(eng, db, _sess(), expiring_exec, log)
    assert _row(db, "a")[2] == "SURVIVED"             # correction applied
    assert _row(db, "b")[2] == "KILLED"               # kept unchanged
    assert eng._last_verify_exec_failed
    assert eng._last_unresolved_job_ids == set()
    assert any("lease expired" in m and "earlier corrections" in m for m in logs)


def test_stale_unresolved_ids_reset_at_sweep_entry(tmp_path: Path) -> None:
    """_run_in_session resets the unresolved ids at entry, so a run that
    aborts before verification does not carry a prior sweep's ids."""
    eng = _local_engine({"engine": "cosmic_ray"})
    eng._last_unresolved_job_ids = {"stale-from-prior-sweep"}
    sess = SimpleNamespace(
        container_mode=False, project_root=tmp_path, env={},
        use_http=False, worker_urls=[], workers=1, _worker_clones=[],
        _run_bases=set(), _run_owner="token")
    # No Cosmic Ray tooling resolves in tmp_path, so the run aborts early.
    try:
        eng._run_in_session(
            sess=sess, target=tmp_path / "m.py", test_dir=tmp_path / "t",
            per_mutant_timeout=10, wall_clock_timeout=60,
            operators=None)
    except Exception:
        pass
    assert eng._last_unresolved_job_ids == set()


def _matrix(eng, db: Path, target: Path) -> dict:
    """The kill matrix of ``db`` as a capture reads it, through JSON."""
    return json.loads(json.dumps(eng._read_mutation_run(db, target).to_kill_matrix()))


def test_clock_verdict_lands_in_the_timeout_bucket(tmp_path: Path) -> None:
    """A kill with output ``timeout`` captures as a timeout, and the score
    still counts it as detected."""
    project_root = tmp_path / "proj"
    session_dir = project_root / "cr"
    session_dir.mkdir(parents=True)
    eng = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    db, _cfg = eng._resolve_paths()
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "t", "test_outcome": "KILLED", "output": "timeout"},
        {"job_id": "c", "test_outcome": "SURVIVED"},
    ])
    matrix = _matrix(eng, db, project_root / "m.py")
    entry = next(m for m in matrix["mutants"] if m["id"] == "t")
    assert entry["status"] == "timeout"
    assert (matrix["summary"]["detected"], matrix["summary"]["timeout"]) == (2, 1)
    assert _kill_matrix.timeout_kill_count(matrix) == 1
    assert _kill_matrix.matrix_score_percent(matrix) == pytest.approx(2 / 3 * 100.0)


def test_zero_collected_reproduction_is_not_genuine(tmp_path: Path) -> None:
    """A zero-collected re-run is the instrument fault reproduced, not a
    genuine kill: the mutant ends unresolved and uncached."""
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "z", "test_outcome": "KILLED", "output": ""},
    ])
    sess = _sess()
    calls = []
    eng, _ = _engine()
    _run_pass(eng, db, sess, _scripted_exec({"z": [
        {"test_outcome": "KILLED", "output": _record("no tests ran in 0.07s", counts={})},
        {"test_outcome": "KILLED", "output": _record("no tests ran in 0.08s", counts={})},
    ]}, calls))
    assert sess.verified_killed_by_all == set()
    assert eng._last_unresolved_job_ids == {"z"}


def test_capture_attaches_timeout_evidence_from_journals(tmp_path: Path) -> None:
    """A clock kill whose journal shows a failing test is captured as an
    attributed kill with its pre_attribution_status, a hang stays
    timeout with the hanging test named."""
    import hashlib
    project_root = tmp_path / "proj"
    session_dir = project_root / "cr"
    session_dir.mkdir(parents=True)
    eng = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    db, _cfg = eng._resolve_paths()

    base = "a = 1\nb = 2\nc = 3\n"
    diff_k = ("--- a/m.py\n+++ b/m.py\n"
              "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 99\n c = 3\n")
    diff_h = ("--- a/m.py\n+++ b/m.py\n"
              "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 77\n c = 3\n")
    from langadapter.python import _mutation as TE
    sha_k = hashlib.sha1(TE.apply_unified_diff(base, diff_k)
                         .encode("utf-8")).hexdigest()
    sha_h = hashlib.sha1(TE.apply_unified_diff(base, diff_h)
                         .encode("utf-8")).hexdigest()

    _make_session(db, [
        {"job_id": "tk", "test_outcome": "KILLED", "output": "timeout",
         "diff": diff_k},
        {"job_id": "th", "test_outcome": "KILLED", "output": "timeout",
         "diff": diff_h},
        {"job_id": "ok", "test_outcome": "KILLED", "output": ATTRIBUTED},
    ])
    evid = session_dir / "evidence"
    (evid / "cr_worker_0").mkdir(parents=True)
    (evid / "baseline_module.txt").write_text(base, encoding="utf-8")
    import json as _j
    (evid / "cr_worker_0" / "journal.jsonl").write_text("\n".join(
        _j.dumps(e) for e in [
            {"run": 1, "module_sha": sha_k},
            {"start": "test_candidate.py::test_kills"},
            {"done": "test_candidate.py::test_kills", "when": "call",
             "outcome": "failed"},
            {"start": "test_candidate.py::test_then_hangs"},
            {"run": 2, "module_sha": sha_h},
            {"start": "test_candidate.py::test_pass"},
            {"done": "test_candidate.py::test_pass", "when": "call",
             "outcome": "passed"},
            {"start": "test_candidate.py::test_hangs"},
        ]) + "\n", encoding="utf-8")

    matrix = _matrix(eng, db, project_root / "m.py")
    rows = {m["id"]: m for m in matrix["mutants"]}
    assert rows["tk"]["status"] == "killed"
    assert rows["tk"]["pre_attribution_status"] == "timeout"
    assert rows["tk"]["killer"] == "test_candidate.py::test_kills"
    assert rows["tk"]["timeout_evidence"] == "named_killer"
    assert rows["th"]["status"] == "timeout"
    assert rows["th"]["killer"] == "test_candidate.py::test_hangs"
    assert rows["th"]["timeout_evidence"] == "hang"
    # all three count as detected
    assert matrix["summary"]["detected"] == 3
    assert matrix["summary"]["timeout"] == 1


def test_missing_verification_row_is_treated_as_no_evidence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    calls = []
    eng, _ = _engine()
    _run_pass(eng, db, _sess(), _scripted_exec({"b": [None, None]}, calls))
    assert len(calls) == 2
    assert eng._last_unresolved_job_ids == {"b"}


def test_exec_failure_keeps_records_and_marks_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    logs = []
    eng, log = _engine(logs)
    sess = _sess()
    _run_pass(eng, db, sess, lambda vdb, n: False, log)
    assert _row(db, "b")[2] == "KILLED"
    assert eng._last_unresolved_job_ids == set()
    assert sess.verified_killed_by_all == set()
    assert any("verification exec failed" in m for m in logs)


def test_a_corrected_db_reads_corrected_in_the_matrix(
    tmp_path: Path,
) -> None:
    """After a transient correction, the captured matrix reads the corrected
    db: the corrected kill is a survivor."""
    project_root = tmp_path / "proj"
    session_dir = project_root / "cr"
    session_dir.mkdir(parents=True)
    eng = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    db, _cfg = eng._resolve_paths()
    _make_session(db, [
        {"job_id": "a", "test_outcome": "KILLED", "output": ATTRIBUTED},
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
        {"job_id": "c", "test_outcome": "SURVIVED"},
    ])
    _run_pass(eng, db, _sess(),
              _scripted_exec({"b": [{"test_outcome": "SURVIVED",
                                     "output": _record("3 passed in 0.2s", counts={"passed": 3})}]}, []))
    matrix = _matrix(eng, db, project_root / "m.py")
    assert matrix["summary"]["detected"] == 1
    assert matrix["summary"]["survived"] == 2
    assert _kill_matrix.matrix_score_percent(matrix) == pytest.approx(100.0 / 3)


def test_capture_marks_unresolved_mutants(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    session_dir = project_root / "cr"
    session_dir.mkdir(parents=True)
    eng = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    db, _cfg = eng._resolve_paths()
    _make_session(db, [
        {"job_id": "b", "test_outcome": "KILLED", "output": ""},
    ])
    _run_pass(eng, db, _sess(), _scripted_exec({"b": [None, None]}, []))
    assert eng._last_unresolved_job_ids == {"b"}
    matrix = _matrix(eng, db, project_root / "m.py")
    assert matrix["summary"]["unresolved"] == 1
    entry = next(m for m in matrix["mutants"] if m["id"] == "b")
    assert entry["unresolved"] is True
    assert _kill_matrix.unresolved_kill_count(matrix) == 1


def test_productive_batches_verify_every_suspect(
    tmp_path: Path, monkeypatch,
) -> None:
    """While each batch produces measurement evidence, further batches
    verify every suspect."""
    monkeypatch.setattr(_cr_verify, "_KILL_VERIFY_MAX", 3)
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": f"j{i}", "test_outcome": "KILLED", "output": ""}
        for i in range(5)
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    script = {f"j{i}": [{"test_outcome": "SURVIVED", "output": "1 passed"}]
              for i in range(5)}
    _run_pass(eng, db, _sess(), _scripted_exec(script, calls), log)
    assert calls == [["j0", "j1", "j2"], ["j3", "j4"]]
    for i in range(5):
        assert _row(db, f"j{i}")[2] == "SURVIVED"
    assert eng._last_unresolved_job_ids == set()
    assert any("5 checked, 5 corrected" in m for m in logs)


def test_zero_evidence_batch_flags_the_remainder(
    tmp_path: Path, monkeypatch,
) -> None:
    """A full batch without measurement evidence stops the pass and flags
    the untried remainder unresolved without execution."""
    monkeypatch.setattr(_cr_verify, "_KILL_VERIFY_MAX", 2)
    db = tmp_path / "s.sqlite"
    _make_session(db, [
        {"job_id": f"j{i}", "test_outcome": "KILLED", "output": ""}
        for i in range(5)
    ])
    logs, calls = [], []
    eng, log = _engine(logs)
    # No scripted results: every verification run dies before recording.
    _run_pass(eng, db, _sess(), _scripted_exec({}, calls), log)
    assert calls == [["j0", "j1"], ["j0", "j1"]]          # one batch, 2 attempts
    assert eng._last_unresolved_job_ids == {f"j{i}" for i in range(5)}
    for i in range(5):
        assert _row(db, f"j{i}")[2] == "KILLED"           # all kept unchanged
    assert any("unresolved without execution" in m for m in logs)


def test_a_clean_matrix_has_no_unresolved_kill() -> None:
    matrix = {"mutants": [
        {"id": "a", "status": "killed", "killer": "t::x"},
        {"id": "b", "status": "survived"},
    ]}
    assert _kill_matrix.unresolved_kill_count(matrix) == 0


def test_pytest_report_detection() -> None:
    assert _pytest_reported(_record("2 failed, 1 passed in 0.34s",
                                    counts={"failed": 2, "passed": 1}))
    assert _pytest_reported(_record("==== 1 error in 0.05s ====", counts={"error": 1}))
    assert _pytest_reported(_record("no tests ran in 0.01s", counts={}))
    # pytest's own text without the record is no report, nor a record from
    # a session that ended before counting.
    assert not _pytest_reported("2 failed, 1 passed in 0.34s")
    assert not _pytest_reported(_record("collecting ...", failed=[], errors=[]))
    assert not _pytest_reported("")
    assert not _pytest_reported(None)
    assert not _pytest_reported("Killed")
    assert not _pytest_reported("collecting ...\nSegmentation fault")


# The unattributed-kill verification pass with real Cosmic Ray.
#
# A module-level ``10 // 1`` gives NumberReplacer a mutant (``1 -> 0``) that
# raises at import, so every test fails at collection: a genuine killed-by-all
# mutant with no named killer. The pass verifies it with a real exec,
# classifies it genuine, caches its identity and leaves nothing unresolved. A
# second sweep in the session skips it by identity despite a new job id.
#
# ``test_kill_verification.py`` covers the transient case with stubs. One
# local worker, skipped without ``cosmic-ray`` beside the interpreter.
_MODULE = (
    "BASE = 10 // 1\n"
    "\n"
    "def times_base(x):\n"
    "    return x * BASE\n"
)
_TESTS = (
    "from brk import times_base\n"
    "def test_times_base():\n    assert times_base(2) == 20\n"
    "def test_times_base_zero():\n    assert times_base(0) == 0\n"
)
_PMT = 120
_WALL = 600


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(),
                    reason="needs cosmic-ray beside the interpreter")
def test_genuine_killed_by_all_verified_and_cached_across_sweeps(tmp_path):
    work = tmp_path / "project"
    test_dir = work / "brk_tests"
    test_dir.mkdir(parents=True)
    target = work / "brk.py"
    target.write_text(_MODULE, encoding="utf-8")
    (test_dir / "test_brk.py").write_text(_TESTS, encoding="utf-8")

    logs: list = []
    context = lb.ProjectContext(
        root=str(work), interpreter=lb.Interpreter(path=sys.executable))
    engine = engine_module.CosmicRayEngine(
        {**CosmicRaySettings()._engine_config("local"), "log_fn": lambda m, level=0: logs.append(m)},
        adapter=PythonAdapter(context))
    with engine.session(project_root=work, workers=1) as sess:
        sess.run(
            target=target, test_dir=test_dir,
            per_mutant_timeout=_PMT, wall_clock_timeout=_WALL,
            operators=["core/NumberReplacer"],
        )
        km = json.loads(json.dumps(engine._read_mutation_run(
            engine._resolve_paths()[0], target).to_kill_matrix()))

        first_sweep_logs = list(logs)
        assert km["summary"]["total"] > 0, "sweep produced no mutants"
        # The import-breaking mutant reproduced with a pytest report: genuine.
        assert any("genuine killed-by-all (cached)" in m
                   for m in first_sweep_logs), first_sweep_logs
        assert not any("kept unresolved" in m or "flagged unresolved" in m
                       for m in first_sweep_logs)
        assert sess.verified_killed_by_all, "identity cache stayed empty"

        assert km["summary"]["unresolved"] == 0
        killed_no_killer = [
            m for m in km["mutants"]
            if m["status"] == "killed" and not m.get("killer")
        ]
        assert killed_no_killer, "expected a killed-by-all mutant"
        assert not any(m.get("unresolved") for m in killed_no_killer)

        # A second sweep: a new job id, but the cached identity leaves
        # nothing to verify.
        logs.clear()
        sess.run(
            target=target, test_dir=test_dir,
            per_mutant_timeout=_PMT, wall_clock_timeout=_WALL,
            operators=["core/NumberReplacer"],
        )
        verify_lines = [m for m in logs if "[kill-verify]" in m]
        assert any("0 unattributed kill(s) to verify" in m
                   and "cached genuine" in m for m in verify_lines), (
            verify_lines)
        assert not any("genuine killed-by-all (cached)" in m for m in logs)
