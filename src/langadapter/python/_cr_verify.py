"""What a Cosmic Ray session's rows mean: each mutant's status and id, the
verification of kills without measurement evidence, the adjudication of
unresolved ones, the instrument fault check and the session read into a
MutationRun."""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple
from langadapter import mutation as _evidence
from langadapter._linux_job import ControlError
from langadapter.sources import bounded
from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, INCOMPETENT, KILLED_STATUS, SKIPPED,
    SKIPPED_STATUS, SURVIVED, SURVIVED_STATUS, TIMEOUT_STATUS, UNTESTED, UNTESTED_STATUS,
    WITHOUT_NAMED_TEST, Classification, MutationClass, MutationProgress, Mutant,
    MutantKey, FileAttribution, MutationRun, NativeRecord, Verification, _summarize,
    mutant_key,
)
from langadapter.python._runs import (
    FILE_ATTRIBUTION, record_collected_nothing, record_killers, record_reported, run_record_in,
    run_record_mark,
)
from langadapter.resources import hardware_usage

if TYPE_CHECKING:
    from langadapter.python._cosmic_ray import CosmicRayEngineSession

#: Batch size of the kill verification. A full batch that resolves nothing
#: signals a systemic fault (a broken instrument, not per-mutant
#: transients): the untried rest is then flagged unresolved, never dropped.
_KILL_VERIFY_MAX = 150


def _pytest_reported(output: Optional[str]) -> bool:
    """Whether a per-mutant run's output holds the run record of a pytest
    session that ended and reported: a genuine broken-source mutant, not a
    process that died before measuring anything."""
    return record_reported(run_record_in(output))


def _killers(output: Optional[str]) -> Tuple[str, ...]:
    """The tests that kill a mutant, as its run record names them
    (``record_killers``)."""
    return record_killers(run_record_in(output))


def _without_record(output: str) -> str:
    """A per-mutant output without the run record's line, as shown or kept."""
    mark = run_record_mark()
    return "\n".join(line for line in output.split("\n") if not line.startswith(mark))


def mutant_id(module_path: Optional[str], operator: Optional[str],
              occurrence: Optional[int]) -> Optional[str]:
    """A mutant's id from what makes it (the module, the operator and its
    occurrence), 32 hexadecimal digits as Cosmic Ray's job ids: the same in
    every run over the same module. None for a record without them."""
    if module_path is None or operator is None or occurrence is None:
        return None
    identity = f"{Path(str(module_path)).as_posix()}\x1f{operator}\x1f{int(occurrence)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _zero_collected(output: Optional[str]) -> bool:
    """Whether a per-mutant run's record shows pytest's "no tests ran": no
    test, error or warning. A mutant never edits the test files, so it can
    break collection into errors but never make pytest collect nothing
    without one: a killed verdict with this record is an instrument fault,
    never a kill."""
    return record_collected_nothing(run_record_in(output))


def _bounded_output(output: str) -> str:
    """A per-mutant test output as a kill matrix keeps it
    (``langadapter.sources.bounded``)."""
    return bounded(output)


def classify_outcome(
    test_outcome: Optional[str],
    worker_outcome: Optional[str],
    output: Optional[str],
    killers: Sequence[str] = (),
) -> Classification:
    """The kill-matrix status, common class and subcategory of a mutant's
    native Cosmic Ray record (its test outcome, worker outcome and output,
    and the killers parsed from that output):

    ======================================  =============  ===========================
    Native record                           Status         Class, subcategory
    ======================================  =============  ===========================
    worker SKIPPED (a filtered operator)    (left out)     not run, skipped
    no result                               untested       not run, untested
    KILLED with the output ``timeout``      timeout        detected, timeout
    KILLED, a killer named                  killed         detected, failing_test
    KILLED, no killer named                 killed         detected, no_named_test
    SURVIVED                                survived       undetected, survived
    INCOMPETENT                             untested       not run, incompetent
    any other result (no test outcome)      untested       not run, untested
    ======================================  =============  ===========================

    Cosmic Ray records a per-mutant timeout as KILLED with the output
    ``timeout``. A kill without a named killer covers crashes and collection
    errors. A result without a test outcome measured nothing (a failed
    worker request is ABNORMAL, an unappliable mutation NO_TEST), and so did
    INCOMPETENT, which Cosmic Ray records when running the tests or applying
    the mutation raised (a mutant that breaks the tests is KILLED). A timeout
    the per-test journal attributes to a failing test later becomes
    ``killed``, keeping its ``pre_attribution_status``
    (``langadapter.python._mutation``)."""
    to = str(test_outcome).upper() if test_outcome else ""
    wo = str(worker_outcome).upper() if worker_outcome else ""
    if wo == "SKIPPED":
        return Classification(SKIPPED_STATUS, MutationClass.NOT_RUN, SKIPPED)
    if not to and not wo:
        return Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED)
    if to == "KILLED":
        if (output or "").strip().lower() == "timeout":
            return Classification(TIMEOUT_STATUS, MutationClass.DETECTED,
                                  BY_TIMEOUT)
        return Classification(KILLED_STATUS, MutationClass.DETECTED,
                              BY_FAILING_TEST if killers else WITHOUT_NAMED_TEST)
    if to == "SURVIVED":
        return Classification(SURVIVED_STATUS, MutationClass.UNDETECTED,
                              SURVIVED)
    if to == "INCOMPETENT":
        return Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, INCOMPETENT)
    # An unknown test outcome: no test ran under the mutant, so it counts as
    # not killed (``cr-rate`` counts it as killed).
    return Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED)


def _mutant_of(
    entry: Dict[str, Any], key: MutantKey, test_outcome: Optional[str],
    worker_outcome: Optional[str], classification: Classification,
) -> Mutant:
    """A kill-matrix row as a typed mutant. A timeout the per-test journal
    attributed to a failing test (``pre_attribution_status``) is detected by
    that test."""
    if (entry.get("pre_attribution_status") is not None
            and entry["status"] == KILLED_STATUS):
        classification = Classification(
            KILLED_STATUS, MutationClass.DETECTED, BY_FAILING_TEST)
    return Mutant(
        key=key, engine_id=entry["id"], test_outcome=test_outcome,
        worker_outcome=worker_outcome, classification=classification,
        killers=tuple(entry.get("killers") or ()), diff=entry.get("diff"),
        row=entry)


class MutationInstrumentFault(RuntimeError):
    """The per-mutant run environment is broken: kills lack measurement
    evidence (a run that collected zero tests, or a verification pass whose
    every re-run died before reporting). A score over them would count
    process deaths as kills, so the run aborts instead of returning one. The
    session is unusable until the environment is fixed."""


def _build_verification_db(
    session_path: Path, job_ids: List[str], dest: Path,
) -> None:
    """Write a verification session at ``dest``: a copy of the sweep's
    session holding only the suspect work items and mutation specs, with
    all results cleared, so ``cosmic-ray exec`` re-runs exactly the
    suspects through the same catalogue entries."""
    with contextlib.suppress(OSError):
        dest.unlink()
    shutil.copy2(str(session_path), str(dest))
    qs = ",".join("?" for _ in job_ids)
    conn = sqlite3.connect(str(dest), timeout=10)
    try:
        conn.execute("DELETE FROM work_results")
        conn.execute(
            f"DELETE FROM mutation_specs WHERE job_id NOT IN ({qs})", job_ids)
        conn.execute(
            f"DELETE FROM work_items WHERE job_id NOT IN ({qs})", job_ids)
        conn.commit()
    finally:
        conn.close()


def _read_result_rows(
    db_path: Path, job_ids: List[str],
) -> Dict[str, Tuple[Any, Any, Any, Any]]:
    """``{job_id: (worker_outcome, output, test_outcome, diff)}`` for the
    given jobs, from ``db_path``'s work_results. Jobs with no result row
    are absent from the dict."""
    qs = ",".join("?" for _ in job_ids)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = conn.execute(
            f"SELECT job_id, worker_outcome, output, test_outcome, diff "
            f"FROM work_results WHERE job_id IN ({qs})", job_ids,
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _poll_session_progress(
    *,
    session_path: Path,
    stop_event: threading.Event,
    interval_seconds: int,
    log_fn,
    start_perf: float,
    progress=None,
    kind: str = "mutants",
    offset: int = 0,
    known_total: Optional[int] = None,
) -> None:
    """Poll the session SQLite until ``stop_event`` is set, logging a
    progress line every ``interval_seconds`` and passing ``progress`` the
    finished and total counts of ``kind`` every second and once more after
    the stop, so results committed after the last read are reported:
    ``offset`` plus the database's finished results, of ``known_total`` when
    the database holds part of the whole (a kill verification batch).
    ``cosmic-ray exec`` commits each result, so a read-only query sees it.
    Errors are swallowed: a polling failure or a failing callback never
    changes the run."""
    logged = start_perf
    stopped = False
    while not stopped:
        stopped = stop_event.wait(1 if progress is not None else interval_seconds)
        if stopped and progress is None:
            break
        try:
            if not session_path.exists() or session_path.stat().st_size == 0:
                continue
            # Closed at once and never waiting long on a writer: the stop
            # join is bounded, and Windows cannot remove an open session file.
            with contextlib.closing(sqlite3.connect(
                f"file:{session_path}?mode=ro", uri=True, timeout=0.5,
            )) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM work_items"
                ).fetchone()[0]
                # A running mutant has no result yet. A per-mutant timeout is
                # KILLED with the output "timeout".
                rows = conn.execute(
                    "SELECT test_outcome, worker_outcome, "
                    "lower(trim(coalesce(output, ''))) = 'timeout', COUNT(*) "
                    "FROM work_results GROUP BY 1, 2, 3"
                ).fetchall()
            killed = survived = timeout = incompetent = done = skipped = 0
            for test_outcome, worker_outcome, clock, n in rows:
                done += n
                to = (str(test_outcome).upper() if test_outcome else "")
                wo = (str(worker_outcome).upper() if worker_outcome else "")
                if wo == "SKIPPED":
                    skipped += n
                if to == "KILLED" and clock:
                    timeout += n
                elif to == "KILLED":
                    killed += n
                elif to == "SURVIVED":
                    survived += n
                elif to == "INCOMPETENT":
                    incompetent += n
            now = time.perf_counter()
            elapsed = now - start_perf
            if progress is not None:
                # Mutants the operator filter skipped are never run.
                measured = total - skipped
                whole = known_total if known_total is not None else (
                    measured if measured > 0 else None)
                try:
                    progress(MutationProgress(kind, offset + done - skipped, whole, elapsed))
                except Exception:  # a callback never changes the run
                    pass
            if stopped or log_fn is None or interval_seconds <= 0 or now - logged < interval_seconds:
                continue
            logged = now
            rate = done / elapsed if elapsed > 0 else 0.0
            hw = hardware_usage()
            log_fn(
                f"[CosmicRayEngine] progress {done}/{total} mutants "
                f"({(100 * done / total) if total else 0:.1f}%): "
                f"killed {killed}, survived {survived}, "
                f"timeout {timeout}, incompetent {incompetent}, "
                f"{elapsed:.0f}s elapsed @ {rate:.1f} mutants/s"
                + (f" | {hw}" if hw else ""),
                level=1,
            )
        except (sqlite3.Error, OSError):
            continue


class _Verification:
    """The engine's reading of its sessions (``CosmicRayEngine``'s base):
    the session as a MutationRun, the instrument fault check, and the
    verification and adjudication of kills without measurement evidence."""

    def _read_mutation_run(
        self, session_path: Path, target: Path, _log=None,
    ) -> MutationRun:
        """The session's mutants as a self-contained run, Cosmic Ray's
        outcomes mapped to kill-matrix statuses (``classify_outcome``) and
        filtered (SKIPPED) mutants left out.

        A mutant's ``id`` is Cosmic Ray's ``job_id``, minted fresh (uuid4) by
        every ``cosmic-ray init``, so it identifies a mutant within one sweep
        only: mutants of different runs are compared by ``key`` (file,
        operator, occurrence, line, column and diff hash). Each carries its
        operator, so a caller can derive an operator subset's score
        (Wong-Mathur, say) from a full-catalogue run.

        Raises ``sqlite3.Error`` when the session cannot be read (the run
        refused kills without measurement evidence before,
        ``_raise_on_instrument_fault``)."""
        with contextlib.closing(
                sqlite3.connect(str(session_path), timeout=10)) as conn:
            rows = conn.execute(
                """
                SELECT wi.job_id, ms.start_pos_row, ms.operator_name,
                       wr.test_outcome, wr.worker_outcome, wr.output,
                       wr.diff, ms.module_path, ms.occurrence, ms.start_pos_col
                FROM work_items wi
                LEFT JOIN mutation_specs ms ON ms.job_id = wi.job_id
                LEFT JOIN work_results wr ON wr.job_id = wi.job_id
                ORDER BY ms.start_pos_row, wi.job_id
                """
            ).fetchall()

        mutants = []
        # Kills whose verification produced no measurement evidence, kept as
        # cr-rate counts them and flagged unresolved.
        unresolved_ids = set(
            getattr(self, "_last_unresolved_job_ids", set()))
        verification = getattr(self, "_last_verification", {})
        adjudication = getattr(self, "_last_adjudication", {})
        # The killers are parsed from the per-mutant output: a test kills a
        # mutant exactly when it fails under it, and a -x run reports one.
        native = {}
        # Results that measured nothing (a failed request to a worker, a
        # mutation not applied, tests that raised), among the untested, as
        # (whether it stamps the matrix incomplete, description).
        without_outcome = []
        for (job_id, line_no, operator_name, test_outcome, worker_outcome,
             output, diff, module_path, occurrence, column) in rows:
            killers = _killers(output) if output else ()
            # A filter-excluded operator is no measured mutant, so it stays
            # out of the matrix and cannot inflate a derived score. A timeout
            # keeps its own status (``killed_under`` treats it by line).
            classification = classify_outcome(
                test_outcome, worker_outcome, output, killers)
            st = classification.status
            if st == SKIPPED_STATUS:
                continue
            if st == UNTESTED_STATUS and (test_outcome or worker_outcome):
                stamps = (str(worker_outcome).upper() == "ABNORMAL"
                          or classification.subcategory == INCOMPETENT)
                without_outcome.append((stamps, (
                    f"{operator_name}@{line_no} ({worker_outcome}"
                    + (f": {str(output).strip()[:200]}" if output else "") + ")")))
            entry = {
                # The job id identifies the mutant within this session. The
                # mutant_id, as PIT's, is the same in every run: the one shown.
                "id": job_id,
                "mutant_id": mutant_id(module_path, operator_name, occurrence) or job_id,
                "line": int(line_no) if line_no is not None else None,
                # Lets a caller derive an operator subset's score by filtering.
                "operator": operator_name,
                "status": st,
                # NORMAL, EXCEPTION, ABNORMAL, TIMEOUT and others. A kill with
                # a non-NORMAL one is a process failure (a crash, a resource
                # kill), not a test failure: a possible false kill.
                "worker_outcome": (
                    str(worker_outcome).upper() if worker_outcome else ""),
            }
            # The diff keeps the matrix self-contained (no session SQLite).
            if diff:
                entry["diff"] = diff
            if output:
                if killers:
                    # ``killer`` is the earliest (the only one under -x).
                    # ``killers`` is every killing test when -x is dropped.
                    entry["killer"] = killers[0]
                    entry["killers"] = list(killers)
                # The bounded raw output traces a verdict to its failure: off
                # by default (large), on with ``cosmic_ray_keep_output``
                # (``CosmicRaySettings.keep_output``). <prefix>MUTATION_DIAG_TB
                # adds tracebacks.
                if self.config["cosmic_ray_keep_output"]:
                    entry["output"] = _bounded_output(output)
            if job_id in unresolved_ids:
                entry["unresolved"] = True
            # What the verification and the adjudication did to the mutant.
            if job_id in verification:
                entry["verification"] = verification[job_id].to_dict()
            if job_id in adjudication:
                entry["adjudication"] = adjudication[job_id].to_dict()
            mutants.append(entry)
            native[id(entry)] = (
                mutant_key(module_path, operator_name, occurrence,
                           line_no, column, diff),
                test_outcome, worker_outcome, classification)
        # Each timeout pairs with its block of the per-test journals (written
        # by the instrument or the journal plugin, beside the baseline
        # module). A run that reported a failing test before the clock fired
        # becomes an attributed kill, a hang names the hanging test, and a
        # cumulative timeout stays unattributed. Both count as detected, so
        # only the timeout/killed split moves.
        evidence_dir = session_path.parent / "evidence"
        baseline_path = evidence_dir / "baseline_module.txt"
        if evidence_dir.is_dir() and baseline_path.is_file():
            from langadapter.python import _mutation as _tev
            baseline = baseline_path.read_text(encoding="utf-8")
            n_ev = _tev.attach_timeout_evidence(
                mutants, evidence_dir, baseline,
                log=(lambda m: _log(m, 2)) if _log else None)
            if n_ev and _log:
                _log(f"[kill-matrix] timeout evidence attached to "
                     f"{n_ev} mutant(s)", 2)
            # A kill without a named killer whose run ended mid-test gains
            # the test that was running then, as an observation. Its status
            # and killers stay.
            n_crash = _tev.attach_crash_observations(
                mutants, evidence_dir, baseline,
                log=(lambda m: _log(m, 2)) if _log else None)
            if n_crash and _log:
                _log(f"[kill-matrix] crash observation attached to "
                     f"{n_crash} mutant(s)", 2)
        # One scored mutant per row, byte-identical sources not deduplicated.
        summary = _summarize(mutants)
        # Logged for inspection: timed-out and unattributed kills stay in the
        # matrix and count as cr-rate counts them.
        if _log:
            killed_no_killer = sum(
                1 for m in mutants
                if m.get("status") == KILLED_STATUS and not m.get("killer")
            )
            if summary["timeout"] or killed_no_killer:
                _log(
                    f"[kill-matrix] non-clean mutants: "
                    f"{summary['timeout']} timeout, "
                    f"{killed_no_killer} killed without an attributed "
                    f"killer, retained in the matrix for inspection.",
                    2,
                )

        # A catalogue with jobs outstanding gets no score: a terminated run
        # leaves the slow mutants, and timeouts count as detected, so the
        # resolved subset's ratio would read high. The verdicts stay. A failed
        # request to a worker (ABNORMAL) or tests that raised (INCOMPETENT)
        # measured nothing either, so they stamp the matrix too. A mutation
        # that could not be applied singles out no slow mutants, so the matrix
        # still scores, that mutant counted as not killed.
        stamp = None
        unmeasured = summary.get("untested", 0) - sum(
            1 for stamps, _ in without_outcome if not stamps)
        if unmeasured:
            stamp = {
                "reason": _evidence.UNTESTED_MUTANTS_IN_CATALOGUE,
                "untested": unmeasured,
                "total": summary["total"],
            }
            if _log:
                _log(
                    f"[kill-matrix] {unmeasured} of "
                    f"{summary['total']} mutants have no result, a failed "
                    f"worker request or tests that raised, so the matrix is "
                    f"stamped incomplete and reports no score",
                    2,
                )
        if without_outcome and _log:
            _log(
                f"[kill-matrix] {len(without_outcome)} of {summary['total']} "
                f"mutants have a result that measured nothing and count as "
                f"untested, first: {without_outcome[0][1]}",
                1,
            )
        return MutationRun(
            target_file=Path(target).name,
            mutants=tuple(
                _mutant_of(entry, *native[id(entry)]) for entry in mutants),
            summary=dict(summary),
            requested_catalogue=self._last_requested_catalogue,
            requested_operators=tuple(self._last_requested_operators),
            incomplete=stamp,
        )

    def _raise_on_instrument_fault(
        self,
        session_path: Path,
        log_fn=None,
        *,
        reject_any_unresolved: bool = False,
    ) -> None:
        """Raise ``MutationInstrumentFault``, before the caller admits a
        score, for a killed row that collected no tests or a verification of
        at least ten suspects that all remain unresolved. With
        ``reject_any_unresolved``, any unresolved suspect or a failed
        verifier raises too. An unreadable session is not checked."""
        try:
            conn = sqlite3.connect(str(session_path), timeout=10)
            try:
                rows = conn.execute(
                    """
                    SELECT ms.operator_name, ms.start_pos_row, wr.output
                    FROM work_results wr
                    LEFT JOIN mutation_specs ms ON ms.job_id = wr.job_id
                    WHERE lower(coalesce(wr.test_outcome, '')) = 'killed'
                      AND lower(coalesce(wr.worker_outcome, '')) != 'skipped'
                    """
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return
        for operator, line, output in rows:
            if _zero_collected(output):
                msg = (
                    f"per-mutant run for {operator}@{line} collected zero "
                    f"tests ('no tests ran'): the worker run environment is "
                    f"broken and its kills are process deaths, not "
                    f"detections. Aborting the mutation run."
                )
                if log_fn:
                    log_fn(f"[instrument-fault] {msg}", 2)
                raise MutationInstrumentFault(msg)
        checked = getattr(self, "_last_verify_checked", 0)
        unresolved = getattr(self, "_last_unresolved_job_ids", set())
        verify_failed = getattr(self, "_last_verify_exec_failed", False)
        if reject_any_unresolved and (unresolved or verify_failed):
            detail = (
                f"{len(unresolved)} suspect kill(s) remained without "
                "measurement evidence"
                if unresolved
                else "the verification executor failed"
            )
            msg = self._unresolved_failure_message.format(detail=detail)
            if log_fn:
                log_fn(f"[instrument-fault] {msg}", 2)
            raise MutationInstrumentFault(msg)
        if checked >= 10 and len(unresolved) >= checked:
            msg = (
                f"all {checked} verified suspect kill(s) produced no "
                f"measurement evidence on re-attempt: the per-mutant "
                f"instrument is not producing verdicts. Aborting the "
                f"mutation run."
            )
            if log_fn:
                log_fn(f"[instrument-fault] {msg}", 2)
            raise MutationInstrumentFault(msg)

    def _query_unattributed_kills(
        self,
        session_path: Path,
    ) -> List[Dict[str, Any]]:
        """The sweep's suspects: killed mutants whose output names no
        ``::``-bearing killer (the matrix's parse), each with its job id,
        cross-sweep identity, line (for logging) and ``clock`` flag. Clock
        verdicts (output ``timeout``) are suspects too: their one re-run
        upgrades a cumulative-slowdown kill to an assertion kill with named
        killers, replaces a transient clock verdict with the fresh survival,
        or reproduces the hang, cached as genuine so it never costs the
        budget again."""
        conn = sqlite3.connect(str(session_path), timeout=10)
        try:
            rows = conn.execute(
                """
                SELECT wr.job_id, ms.module_path, ms.operator_name,
                       ms.occurrence, ms.start_pos_row, wr.output,
                       wr.test_outcome, wr.worker_outcome, wr.diff
                FROM work_results wr
                LEFT JOIN mutation_specs ms ON ms.job_id = wr.job_id
                WHERE lower(coalesce(wr.test_outcome, '')) = 'killed'
                      AND lower(coalesce(wr.worker_outcome, '')) != 'skipped'
                ORDER BY ms.start_pos_row, wr.job_id
                """
            ).fetchall()
        finally:
            conn.close()
        suspects = []
        for (job_id, module_path, operator, occurrence, line, output,
             test_outcome, worker_outcome, diff) in rows:
            if _killers(output):
                continue
            # The join yields no spec only for a corrupt or partial session:
            # the row is verified with identity None, which _remember_genuine
            # never caches (a shared None key would skip later such suspects).
            identity = (
                (module_path, operator, occurrence)
                if module_path is not None else None
            )
            suspects.append({
                "job_id": job_id,
                "identity": identity,
                "operator": operator or "?",
                "line": line,
                "clock": (output or "").strip().lower() == "timeout",
                "test_outcome": test_outcome,
                "worker_outcome": worker_outcome,
                "output": output or "",
                "diff": diff,
            })
        return suspects

    def _verify_unattributed_kills(
        self,
        *,
        session_path: Path,
        sess: "CosmicRayEngineSession",
        exec_verify,
        suite: Optional[str],
        log_fn=None,
    ) -> None:
        """Re-measure every kill that names no killing test. Mutation is a
        function of suite and source, so a fresh result that disagrees
        replaces the record. ``suite`` is the measured suite's fingerprint
        (``_suite_fingerprint``), keying the session's cache of genuine
        kills. None caches nothing.

        Clock verdicts are suspects too, since a timeout names no killer.
        The re-run separates three cases: a cumulative-slowdown mutant fails
        by assertion and gains named killers, a transient clock verdict
        completes and its survival replaces it, and a genuine hang times out
        again and is cached (its row names no killer, the per-test journal
        can name the hanging test when the matrix is captured).

        A run that dies before pytest reports is recorded KILLED with no node
        id, which killed-by-all semantics would credit to every test. A
        reproduced kill is genuine (and cached) only when pytest reported.
        One without measurement evidence gets one immediate re-attempt and
        is otherwise kept unchanged, uncached and counted as ``unresolved``.
        Suspects are verified in batches of ``_KILL_VERIFY_MAX``. A batch
        that resolves nothing flags the untried rest unresolved.

        ``exec_verify(verify_db, n_suspects) -> bool`` runs the pending
        items of ``verify_db`` in the caller's execution mode. It must not
        raise (a failed verification costs correction, never the run),
        except for a run that leaves its command or the module under test
        unresolved, which ends the pass and the run (``_unsettled``).

        Each suspect's verification is recorded in ``_last_verification``
        (a ``Verification`` per job id): the verdict, the sweep's record,
        each attempt's record and whether a fresh record replaced the
        sweep's."""
        self._last_unresolved_job_ids = set()
        self._last_verify_checked = 0
        self._last_verify_exec_failed = False
        self._last_verification = {}
        try:
            suspects = self._query_unattributed_kills(session_path)
        except sqlite3.Error as exc:
            self._last_verify_exec_failed = True
            if log_fn:
                log_fn(
                    f"[kill-verify] WARNING: suspect query failed "
                    f"({type(exc).__name__}: {exc}), pass skipped.", 1)
            return
        if not suspects:
            return
        cache = getattr(sess, "verified_killed_by_all", set())

        def _cached(identity) -> bool:
            return suite is not None and (suite, identity) in cache

        def _remember_genuine(identity) -> None:
            # A None identity (a spec-less row) is not stable across sweeps,
            # so it is never cached. A kill is genuine only for its suite.
            if identity is not None and suite is not None:
                cache.add((suite, identity))

        todo = [s for s in suspects if not _cached(s["identity"])]
        n_cached = len(suspects) - len(todo)
        n_clock = sum(1 for s in todo if s.get("clock"))
        verdicts: Dict[str, str] = {
            s["job_id"]: _evidence.CACHED_GENUINE
            for s in suspects if _cached(s["identity"])}
        attempts: Dict[str, List[Optional[NativeRecord]]] = {}
        replacements: Dict[str, Tuple[Any, Any, Any, Any]] = {}
        flushed = False

        def _record() -> None:
            # A verdict the pass did not reach (the executor failed, or the
            # pass stopped) is not_verified. A record counts as replaced
            # only once the fresh one is written to the session.
            self._last_verification = {
                s["job_id"]: Verification(
                    verdict=verdicts.get(
                        s["job_id"], _evidence.NOT_VERIFIED),
                    clock=bool(s.get("clock")),
                    recorded=self._native_record(
                        s["worker_outcome"], s["output"],
                        s.get("test_outcome")),
                    attempts=tuple(attempts.get(s["job_id"], ())),
                    replaced=flushed and s["job_id"] in replacements)
                for s in suspects}

        if log_fn:
            message = (
                f"[kill-verify] {len(todo)} unattributed kill(s) to "
                f"verify ({n_clock} clock verdict(s), {n_cached} cached "
                "genuine)"
            )
            log_fn(message, 1)
        if not todo:
            _record()
            return
        if len(todo) > _KILL_VERIFY_MAX and log_fn:
            log_fn(
                f"[kill-verify] {len(todo)} suspects, verified in batches "
                f"of {_KILL_VERIFY_MAX} while each batch keeps producing "
                f"measurement evidence.", 1)

        def _tag(s) -> str:
            line = s["line"] if s["line"] is not None else "?"
            clock = " (clock)" if s.get("clock") else ""
            return f"{s['operator']}@{line}{clock}"

        n_corrected = n_recovered = n_genuine = n_checked = 0
        exec_failed = False
        remaining = todo
        batch_no = 0
        progress = self.config.get("_progress")
        progress_started = time.perf_counter()
        progress_sent = [-1]

        def report(record: MutationProgress) -> None:
            # Counts never fall: a re-attempt's poll starts below the last.
            if progress is None or record.finished < progress_sent[0]:
                return
            progress_sent[0] = record.finished
            try:
                progress(record)
            except Exception:  # a callback never changes the run
                pass

        def report_checked() -> None:
            report(MutationProgress("kill verifications", n_checked, len(todo),
                                    time.perf_counter() - progress_started))

        report_checked()


        try:
            while remaining and not exec_failed:
                batch_no += 1
                pending = remaining[:_KILL_VERIFY_MAX]
                remaining = remaining[_KILL_VERIFY_MAX:]
                n_checked += len(pending)
                resolved_before = n_corrected + n_recovered + n_genuine
                for attempt in (1, 2):
                    if not pending:
                        break
                    vdb = session_path.with_name(
                        session_path.name + f".verify{batch_no}_{attempt}")
                    rows: Dict[str, Tuple[Any, Any, Any, Any]] = {}
                    try:
                        _build_verification_db(
                            session_path, [s["job_id"] for s in pending], vdb)
                        poll_stop = threading.Event()
                        poller = None
                        if progress is not None and attempt == 1:
                            poller = threading.Thread(
                                target=_poll_session_progress,
                                kwargs={"session_path": vdb, "stop_event": poll_stop,
                                        "interval_seconds": 0, "log_fn": None,
                                        "start_perf": progress_started, "progress": report,
                                        "kind": "kill verifications",
                                        "offset": n_checked - len(pending),
                                        "known_total": len(todo)},
                                name="cr-verify-progress-poll", daemon=True)
                            poller.start()
                        try:
                            verified = exec_verify(vdb, len(pending))
                        finally:
                            poll_stop.set()
                            if poller is not None:
                                poller.join(timeout=2.0)
                        if verified:
                            rows = _read_result_rows(
                                vdb, [s["job_id"] for s in pending])
                        else:
                            exec_failed = True
                            self._last_verify_exec_failed = True
                            if log_fn:
                                log_fn(
                                    "[kill-verify] WARNING: verification exec "
                                    "failed, unverified records kept unchanged.",
                                    1)
                    except ControlError as exc:
                        # The run ended incomplete (its supervisor's lease
                        # expired): a failed exec. Earlier corrections flush.
                        if self._unsettled(exc, sess):
                            raise
                        exec_failed = True
                        self._last_verify_exec_failed = True
                        if log_fn:
                            log_fn(
                                f"[kill-verify] WARNING: verification exec failed "
                                f"({exc}), unverified records kept, earlier "
                                f"corrections still applied.", 1)
                    except (OSError, sqlite3.Error) as exc:
                        # A session copy or read failed (disk pressure, an
                        # SQLite I/O error): a failed exec. Earlier corrections
                        # still flush.
                        if self._unsettled(exc, sess):
                            raise
                        exec_failed = True
                        self._last_verify_exec_failed = True
                        if log_fn:
                            log_fn(
                                f"[kill-verify] WARNING: verification db operation "
                                f"failed ({type(exc).__name__}: {exc}), "
                                f"unverified records kept, earlier corrections "
                                f"still applied.", 1)
                    finally:
                        with contextlib.suppress(OSError):
                            vdb.unlink()
                    # Rows a failure could not verify stay unchanged, unmarked.
                    if exec_failed:
                        break
                    still_pending = []
                    for s in pending:
                        row = rows.get(s["job_id"])
                        attempts.setdefault(s["job_id"], []).append(
                            self._native_record(row[0], row[1], row[2])
                            if row is not None else None)
                        if row is None:
                            # No result (the process died first): no evidence.
                            still_pending.append(s)
                            continue
                        worker_outcome, output, test_outcome, _diff = row
                        outcome = str(test_outcome or "").lower()
                        out_text = (output or "").strip().lower()
                        if outcome == "survived":
                            # A completed non-kill: the sweep's kill was
                            # transient and is replaced. A row that measured
                            # nothing (INCOMPETENT, or no test outcome after a
                            # worker ABNORMAL) takes the no-evidence path.
                            replacements[s["job_id"]] = row
                            verdicts[s["job_id"]] = _evidence.CORRECTED
                            n_corrected += 1
                            if log_fn:
                                log_fn(
                                    f"[kill-verify] {_tag(s)}: transient "
                                    f"(verification run: {outcome}) -> replaced",
                                    1)
                            continue
                        if outcome == "killed" and out_text == "timeout":
                            # The clock verdict reproduced: a hanging mutant,
                            # cached so later sweeps do not spend its budget.
                            n_genuine += 1
                            verdicts[s["job_id"]] = (
                                _evidence.REPRODUCED_TIMEOUT)
                            _remember_genuine(s["identity"])
                            if log_fn:
                                log_fn(
                                    f"[kill-verify] {_tag(s)}: reproduced as a "
                                    f"per-mutant timeout -> kept (clock verdict, "
                                    f"cached)", 1)
                            continue
                        if outcome == "killed":
                            killers = _killers(output)
                            if killers:
                                replacements[s["job_id"]] = row
                                verdicts[s["job_id"]] = _evidence.RECOVERED
                                n_recovered += 1
                                if log_fn:
                                    log_fn(
                                        f"[kill-verify] {_tag(s)}: attribution "
                                        f"recovered -> killed by {killers[0]}",
                                        1)
                                continue
                            completed_report = (
                                _pytest_reported(output)
                                and not _zero_collected(output)
                            )
                            worker_report_is_admissible = (
                                str(worker_outcome or "").upper() == "NORMAL"
                            )
                            if completed_report and worker_report_is_admissible:
                                # pytest reported, tests collected, none named:
                                # a genuine broken-source killed-by-all mutant.
                                n_genuine += 1
                                verdicts[s["job_id"]] = (
                                    _evidence.REPRODUCED_WITH_REPORT)
                                _remember_genuine(s["identity"])
                                if log_fn:
                                    log_fn(
                                        f"[kill-verify] {_tag(s)}: reproduced "
                                        f"with report -> genuine killed-by-all "
                                        f"(cached)", 1)
                                continue
                        still_pending.append(s)
                    pending = still_pending
                if exec_failed:
                    break
                for s in pending:
                    self._last_unresolved_job_ids.add(s["job_id"])
                    verdicts[s["job_id"]] = _evidence.UNRESOLVED
                    if log_fn:
                        log_fn(
                            f"[kill-verify] {_tag(s)}: no measurement "
                            f"evidence after re-attempt, kept unresolved",
                            1)
                if (n_corrected + n_recovered + n_genuine
                        == resolved_before) and remaining:
                    # A batch without measurement evidence means the
                    # instrument gives no verdicts: the rest is flagged, not run.
                    for s in remaining:
                        self._last_unresolved_job_ids.add(s["job_id"])
                        verdicts[s["job_id"]] = _evidence.NOT_ATTEMPTED
                    if log_fn:
                        log_fn(
                            f"[kill-verify] WARNING: a full batch produced no "
                            f"measurement evidence. The remaining "
                            f"{len(remaining)} suspect(s) are flagged "
                            f"unresolved without execution.", 1)
                    remaining = []
            if replacements:
                try:
                    conn = sqlite3.connect(str(session_path), timeout=10)
                    try:
                        conn.executemany(
                            "UPDATE work_results SET worker_outcome = ?, "
                            "output = ?, test_outcome = ?, diff = ? "
                            "WHERE job_id = ?",
                            [(w, o, t, d, jid)
                             for jid, (w, o, t, d) in replacements.items()],
                        )
                        conn.commit()
                        flushed = True
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    if log_fn:
                        log_fn(
                            f"[kill-verify] WARNING: session write failed "
                            f"({type(exc).__name__}: {exc}), "
                            f"{len(replacements)} correction(s) not applied.",
                            1)
            report_checked()
            _record()
            if log_fn:
                tail = " (exec failed, some suspects unverified)" if (
                    exec_failed) else ""
                log_fn(
                    f"[kill-verify] pass done: {n_checked} checked, "
                    f"{n_corrected} corrected, {n_recovered} recovered, "
                    f"{n_genuine} genuine, "
                    f"{len(self._last_unresolved_job_ids)} unresolved{tail}",
                    1)
            self._last_verify_checked = n_checked
        except Exception as exc:
            if self._unsettled(exc, sess):
                raise
            self._last_verify_exec_failed = True
            _record()
            if log_fn:
                log_fn(
                    f"[kill-verify] WARNING: pass failed "
                    f"({type(exc).__name__}: {exc}), original records kept.",
                    1)

    def _adjudicate_unresolved_per_file(
        self,
        *,
        session_path: Path,
        exec_adjudicate,
        test_files: List[Path],
        log_fn=None,
        sess=None,
    ) -> None:
        """Attribute the verification's unresolved kills per test file.

        An unresolved kill reproduces without parseable output (the run dies
        before pytest reports), so no re-run names its killers. Against each
        test file alone, a completed KILLED result means that file kills the
        mutant, SURVIVED that it does not, and any other result (INCOMPETENT
        among them) leaves that file unmeasured. A suspect decided
        for every file gets its killers written into the session as standard
        ``FAILED <file>::...`` lines, which killer parsing, the kill matrix
        and attribution read unchanged, and loses its unresolved flag. One
        with a file left unmeasured, or that no file kills alone (a
        pool-context kill), stays unresolved.

        ``exec_adjudicate(vdb, n_suspects, test_file) -> bool`` runs the
        pending items of ``vdb`` in the caller's execution mode with a test
        command scoped to ``test_file``. It must not raise, except for a run
        that leaves its command or the module under test unresolved, which
        ends the pass and the run before another run starts (``_unsettled``).

        Each suspect's adjudication is recorded in ``_last_adjudication`` (a
        ``FileAttribution`` per job id): the outcome and every file's
        verdict."""
        self._last_adjudication = {}
        suspects = sorted(self._last_unresolved_job_ids)
        if not suspects or len(test_files) < 2:
            return
        if log_fn:
            log_fn(
                f"[adjudicate] {len(suspects)} unresolved kill(s) x "
                f"{len(test_files)} test file(s): per-file adjudication", 1)
        verdicts: Dict[str, Dict[str, Optional[bool]]] = {
            j: {} for j in suspects}
        try:
            for tf in test_files:
                vdb = session_path.with_name(
                    session_path.name + f".adjudicate_{tf.stem}")
                rows: Dict[str, Tuple[Any, Any, Any, Any]] = {}
                try:
                    _build_verification_db(session_path, suspects, vdb)
                    if exec_adjudicate(vdb, len(suspects), tf):
                        rows = _read_result_rows(vdb, suspects)
                except (OSError, sqlite3.Error, ControlError) as exc:
                    # No verdict for this file: a failed db operation or a run
                    # that ended without completing (its lease expired).
                    if self._unsettled(exc, sess):
                        raise
                    if log_fn:
                        log_fn(
                            f"[adjudicate] WARNING: run or db operation failed for "
                            f"{tf.name} ({type(exc).__name__}: {exc})", 1)
                finally:
                    with contextlib.suppress(OSError):
                        vdb.unlink()
                for j in suspects:
                    row = rows.get(j)
                    if row is None:
                        verdicts[j][tf.name] = None
                        continue
                    outcome = str(row[2] or "").lower()
                    if outcome == "killed":
                        verdicts[j][tf.name] = True
                    elif outcome == "survived":
                        verdicts[j][tf.name] = False
                    else:
                        verdicts[j][tf.name] = None

            updates: Dict[str, str] = {}
            outcomes: Dict[str, str] = {}
            n_incomplete = n_pool_context = 0
            for j in suspects:
                v = verdicts[j]
                if len(v) < len(test_files) or any(
                        x is None for x in v.values()):
                    n_incomplete += 1
                    outcomes[j] = _evidence.ATTRIBUTION_INCOMPLETE
                    if log_fn:
                        log_fn(f"[adjudicate] {j}: incomplete evidence, "
                               f"kept unresolved", 1)
                    continue
                killers = [n for n, killed in sorted(v.items()) if killed]
                if not killers:
                    n_pool_context += 1
                    outcomes[j] = _evidence.POOL_CONTEXT
                    if log_fn:
                        log_fn(f"[adjudicate] {j}: no single file kills it "
                               f"(a pool-context kill), kept unresolved", 1)
                    continue
                outcomes[j] = _evidence.ATTRIBUTED
                # The killing files' record, which names them as killers
                # and no pytest session that reported.
                named = [f"{n}::{FILE_ATTRIBUTION}" for n in killers]
                updates[j] = "".join(f"FAILED {n}\n" for n in named) + (
                    f"= {len(killers)} failed (per-file adjudication) =\n"
                    + run_record_mark()
                    + json.dumps({"failed": named, "errors": []}))
                if log_fn:
                    log_fn(
                        f"[adjudicate] {j}: killed by {len(killers)}/"
                        f"{len(test_files)} file(s) alone "
                        f"({', '.join(killers[:4])}"
                        f"{', ...' if len(killers) > 4 else ''})", 1)
            if updates:
                conn = sqlite3.connect(str(session_path), timeout=10)
                try:
                    conn.executemany(
                        "UPDATE work_results SET output = ? "
                        "WHERE job_id = ?",
                        [(o, j) for j, o in updates.items()])
                    conn.commit()
                finally:
                    conn.close()
                self._last_unresolved_job_ids -= set(updates)
            self._last_adjudication = {
                j: FileAttribution(outcomes[j], tuple(sorted(
                    verdicts[j].items()))) for j in suspects}
            if log_fn:
                log_fn(
                    f"[adjudicate] pass done: {len(updates)} adjudicated, "
                    f"{n_incomplete} incomplete, {n_pool_context} "
                    f"pool-context, "
                    f"{len(self._last_unresolved_job_ids)} unresolved "
                    f"remain", 1)
        except Exception as exc:
            if self._unsettled(exc, sess):
                raise
            # A pass that failed before recording its outcomes keeps the
            # records, with the verdicts measured so far.
            if not self._last_adjudication:
                self._last_adjudication = {
                    j: FileAttribution(_evidence.ATTRIBUTION_FAILED, tuple(sorted(
                        verdicts[j].items()))) for j in suspects}
            if log_fn:
                log_fn(
                    f"[adjudicate] WARNING: pass failed "
                    f"({type(exc).__name__}: {exc}), records kept.", 1)
