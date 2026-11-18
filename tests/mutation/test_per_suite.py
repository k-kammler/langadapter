"""Per-suite verdicts: the request, the reference path, alignment and checks."""
from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from langadapter.conformance import check_per_suite
from langadapter.errors import InvalidRequest
from langadapter.mutation import INCOMPLETE_KEY
from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, NON_VIABLE, SURVIVED, UNTESTED,
    WITHOUT_NAMED_TEST, Classification, MutationClass,
)
from langadapter.mutation import (
    COLLECTION_ERROR, DEFAULT_IMPLEMENTATION, KILLED_STATUS, NON_VIABLE_STATUS, UNTESTED_STATUS,
    BudgetedSuite, PerSuiteRequest, SuiteVerdict, _verdict_of,
)
from langadapter.mutation import Catalogue, MutationRequest, TimeoutRule
from langadapter.mutation import Mutant, MutationRun, mutant_key
from langadapter.mutation import MutationSession
from langadapter.outcomes import Outcome
from langadapter.suites import Suite, TestId

CLASSES = {
    "killed": Classification("killed", MutationClass.DETECTED, BY_FAILING_TEST),
    "crash": Classification("killed", MutationClass.DETECTED, WITHOUT_NAMED_TEST),
    "timeout": Classification("timeout", MutationClass.DETECTED, BY_TIMEOUT),
    "invalid": Classification("non_viable", MutationClass.DETECTED, NON_VIABLE),
    "survived": Classification("survived", MutationClass.UNDETECTED, SURVIVED),
    "untested": Classification("untested", MutationClass.NOT_RUN, UNTESTED),
}
#: Two mutants by the same operator on one line, told apart by occurrence.
FIRST = mutant_key("target.py", "AddToSub", 0, 3, 8, "-a + b\n+a - b")
SECOND = mutant_key("target.py", "AddToSub", 1, 3, 12, "-b + c\n+b - c")
THIRD = mutant_key("target.py", "Negate", 0, 5, 4, "-x\n+not x")


def mutant(key, kind, killers=(), engine_id=None):
    classification = CLASSES[kind]
    return Mutant(key=key, engine_id=engine_id or f"job-{key.occurrence}-{key.line}",
                  test_outcome=None, worker_outcome=None, classification=classification,
                  killers=tuple(killers), diff=f"diff of {key.operator}",
                  row={"id": engine_id or f"job-{key.occurrence}-{key.line}",
                       "line": key.line, "operator": key.operator,
                       "status": classification.status, "killer": killers[0] if killers else None,
                       "killers": list(killers), "diff": f"diff of {key.operator}",
                       "verification": {"verdict": "genuine"}})


def run_of(*mutants, outcome=Outcome.COMPLETED):
    if outcome is not Outcome.COMPLETED:
        return MutationRun.failed("target.py", outcome, "the suite's run failed")
    return MutationRun("target.py", tuple(mutants), {}, "full", ())


@pytest.fixture
def suites(tmp_path):
    made = []
    for name in ("alpha", "beta", "gamma"):
        root = tmp_path / name
        root.mkdir()
        (root / f"test_{name}.py").write_text("def test_value(): pass\n", encoding="utf-8")
        made.append(BudgetedSuite(name, Suite(str(root), (f"test_{name}.py",)), 7))
    return tuple(made)


class Session(MutationSession):
    """Answers each suite's standalone request from ``table``, by suite root."""

    def __init__(self, table, invalid=None):
        super().__init__(workers_requested=1, workers_started=1)
        self.table = table
        self.invalid = invalid
        self.run_calls = []
        self.close_calls = 0
        self.interleaved = None

    def _validate_request(self, request):
        request.validate()
        if request.suite.root == self.invalid:
            raise InvalidRequest("suite", "the session cannot run this suite")

    def measure(self, request):
        self.run_calls.append(request)
        try:
            self.run(request)
        except InvalidRequest as refusal:
            self.interleaved = str(refusal)
        return self.table[request.suite.root]

    def release(self):
        self.close_calls += 1


class FastSession(Session):
    """A faster implementation that answers from its own table."""

    def __init__(self, table, fast_table, error=None):
        super().__init__(table)
        self.fast_table = fast_table
        self.error = error

    def per_suite_fast(self, request):
        if self.error is not None:
            raise self.error
        return ({suite.name: self.fast_table[suite.suite.root] for suite in request.suites},
                "shared_mutant")


def request_for(suites, **settings):
    return PerSuiteRequest("src/target.py", suites, **settings)


def test_the_request_refuses_what_no_standalone_run_could_take(suites):
    with pytest.raises(InvalidRequest, match="at least one"):
        request_for(()).validate()
    with pytest.raises(InvalidRequest, match="unique: alpha"):
        request_for((suites[0], suites[0])).validate()
    for timeout in (0, True, "5", 1.5):
        with pytest.raises(InvalidRequest, match="per_mutant_timeout"):
            request_for((replace(suites[0], per_mutant_timeout=timeout),)).validate()
    with pytest.raises(InvalidRequest, match="name"):
        request_for((replace(suites[0], name=" "),)).validate()
    with pytest.raises(InvalidRequest, match="attribution"):
        request_for(suites, attribution="some").validate()
    with pytest.raises(InvalidRequest, match="no suite is named"):
        request_for(suites).standalone("delta")


def test_each_suite_is_defined_by_its_standalone_request(suites):
    catalogue = Catalogue(operators=("AddToSub",))
    request = request_for(suites, catalogue=catalogue, attribution="all", wall_clock_limit_s=90,
                          workers=2, fail_on_unresolved=True, timeout_journal=False)
    assert request.standalone("beta") == MutationRequest(
        "src/target.py", suites[1].suite, catalogue, per_mutant_timeout=7,
        wall_clock_limit_s=90, attribution="all", workers=2, fail_on_unresolved=True,
        timeout_journal=False)
    # A rule applied to the suite's own green time, or the engine's own rule.
    rule = TimeoutRule(factor=2.0, minimum_s=5)
    ruled = request_for((replace(suites[0], per_mutant_timeout=rule, passing_suite_s=3.5),
                         replace(suites[1], per_mutant_timeout=None)))
    ruled.validate()
    assert ruled.standalone("alpha") == MutationRequest(
        "src/target.py", suites[0].suite, per_mutant_timeout=rule, passing_suite_s=3.5)
    assert ruled.standalone("beta").per_mutant_timeout is None


@pytest.mark.parametrize("kind,verdict", [
    ("killed", KILLED_STATUS), ("crash", COLLECTION_ERROR),
    ("timeout", "timeout"), ("invalid", NON_VIABLE_STATUS), ("survived", "survived"),
    ("untested", UNTESTED_STATUS)])
def test_each_common_class_has_its_verdict(kind, verdict):
    killers = ("test_a.py::test_one",) if kind in ("killed", "timeout") else ()
    assert _verdict_of(mutant(FIRST, kind, killers)) == SuiteVerdict(verdict, killers)


def test_a_verdict_names_the_killing_tests_its_run_recorded():
    """A suite's verdict carries the tests its run recorded as the mutant's
    killers, so its kills attribute to them (LangAdapter.pool_kills)."""
    test = TestId("test_a.py", "test_one")
    killed = dataclasses.replace(mutant(FIRST, "killed", ("test_a.py::test_one",)),
                                 killing_tests=(test,))
    verdict = _verdict_of(killed)
    assert verdict.killing_tests == (test,)
    assert verdict.to_dict() == {"verdict": KILLED_STATUS, "killers": ["test_a.py::test_one"],
                                 "killing_tests": [dataclasses.asdict(test)]}
    assert _verdict_of(mutant(FIRST, "survived")).to_dict() == {"verdict": "survived", "killers": []}


def test_the_reference_runs_every_standalone_request_in_one_operation(suites):
    table = {suite.suite.root: run_of(mutant(FIRST, "killed", ("t::a",)), mutant(SECOND, "survived"))
             for suite in suites}
    session = Session(table)
    request = request_for(suites)
    result = session.per_suite(request)
    assert session.run_calls == [request.standalone(name) for name in request.names]
    assert session.interleaved == "session: another operation is active"
    assert result.implementation == DEFAULT_IMPLEMENTATION
    assert result.outcome is Outcome.COMPLETED and result.reason == ""
    assert list(result.runs) == ["alpha", "beta", "gamma"]
    assert all(run.resolution.requested == "measure" for run in result.runs.values())
    assert [m.key for m in result.mutants] == [FIRST, SECOND]
    assert result.verdicts("beta") == {FIRST: SuiteVerdict(KILLED_STATUS, ("t::a",)),
                                       SECOND: SuiteVerdict("survived")}


def test_mutants_align_by_key_and_a_failed_suite_keeps_the_others(suites):
    alpha, beta, gamma = (suite.suite.root for suite in suites)
    # The same operator twice on one line, listed in another order by beta.
    table = {alpha: run_of(mutant(FIRST, "killed", ("t::a",)), mutant(SECOND, "survived"),
                           mutant(THIRD, "timeout")),
             beta: run_of(mutant(SECOND, "killed", ("t::b",)), mutant(FIRST, "survived")),
             gamma: run_of(outcome=Outcome.INFRASTRUCTURE_FAILURE)}
    result = Session(table).per_suite(request_for(suites))
    by_key = {m.key: m.verdicts for m in result.mutants}
    assert by_key[FIRST] == {"alpha": SuiteVerdict(KILLED_STATUS, ("t::a",)),
                             "beta": SuiteVerdict("survived"), "gamma": SuiteVerdict(UNTESTED_STATUS)}
    assert by_key[SECOND]["beta"] == SuiteVerdict(KILLED_STATUS, ("t::b",))
    assert by_key[THIRD]["beta"] == SuiteVerdict(UNTESTED_STATUS)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "1 of 3 suites did not complete: gamma (infrastructure_failure" in result.reason
    assert result.runs["gamma"].outcome is Outcome.INFRASTRUCTURE_FAILURE


def test_a_key_repeated_within_one_run_gets_no_verdict_there(suites):
    alpha, beta, gamma = (suite.suite.root for suite in suites)
    table = {alpha: run_of(mutant(FIRST, "killed", ("t::a",)), mutant(FIRST, "survived", engine_id="x")),
             beta: run_of(mutant(FIRST, "survived")), gamma: run_of(mutant(FIRST, "survived"))}
    result = Session(table).per_suite(request_for(suites))
    assert result.mutants[0].verdicts["alpha"] == SuiteVerdict(UNTESTED_STATUS)
    assert "1 mutant key(s) occur twice" in result.reason


@pytest.mark.parametrize("kinds,status", [
    (("killed", "survived", "survived"), "killed"),
    (("crash", "survived", "untested"), "killed"),
    (("timeout", "untested", "survived"), "untested"),
    (("timeout", "survived", "survived"), "timeout"),
    (("invalid", "invalid", "invalid"), "non_viable"),
    (("invalid", "untested", "survived"), "untested"),
    (("survived", "survived", "survived"), "survived"),
])
def test_the_union_row_follows_the_verdict_order(suites, kinds, status):
    table = {suite.suite.root: run_of(mutant(FIRST, kind, ("t::" + suite.name,)
                                             if kind == "killed" else ()))
             for suite, kind in zip(suites, kinds)}
    matrix = Session(table).per_suite(request_for(suites)).kill_matrix()
    (row,) = matrix["mutants"]
    assert row["status"] == status
    assert set(row) == {"id", "line", "operator", "diff", "status", "key", "killer",
                        "killers", "suite_verdicts"}
    # The mutant's key names it in every run of the target, whatever the
    # engine's ids, so a derived and a measured matrix compare by it.
    assert row["key"] == dataclasses.asdict(FIRST)
    assert (INCOMPLETE_KEY in matrix) == (status == "untested")


def test_a_mutant_that_did_not_load_stays_detected_beside_a_suite_without_result(suites):
    """A C mutant under which the Python extension did not load (the native
    status load_error) is non_viable for its suite. The load error belongs to
    the mutant, not to a suite, so a suite without a result for it does not
    make the union row untested, and the union stays complete."""
    alpha, beta, gamma = (suite.suite.root for suite in suites)
    invalid = mutant(FIRST, "invalid")
    did_not_load = replace(invalid, row={**invalid.row, "native_status": "load_error",
                                         "load_error": "the extension did not load"})
    table = {alpha: run_of(did_not_load), beta: run_of(outcome=Outcome.INFRASTRUCTURE_FAILURE),
             gamma: run_of(mutant(FIRST, "survived"))}
    matrix = Session(table).per_suite(request_for(suites)).kill_matrix()
    (row,) = matrix["mutants"]
    assert row["status"] == NON_VIABLE_STATUS
    assert row["suite_verdicts"]["beta"]["verdict"] == UNTESTED_STATUS
    assert INCOMPLETE_KEY not in matrix and matrix["summary"]["untested"] == 0


def test_the_union_lists_every_killer_and_each_suite_keeps_its_matrix(suites):
    alpha, beta, gamma = (suite.suite.root for suite in suites)
    table = {alpha: run_of(mutant(FIRST, "killed", ("t::a", "t::b"))),
             beta: run_of(mutant(FIRST, "killed", ("t::b", "t::c"))),
             gamma: run_of(mutant(FIRST, "survived"))}
    result = Session(table).per_suite(request_for(suites, attribution="all"))
    (row,) = result.kill_matrix()["mutants"]
    assert row["killers"] == ["t::a", "t::b", "t::c"] and row["killer"] == "t::a"
    assert row["suite_verdicts"]["gamma"] == {"verdict": "survived", "killers": []}
    assert result.kill_matrix()["per_suite"] == {
        "implementation": DEFAULT_IMPLEMENTATION, "suites": ["alpha", "beta", "gamma"],
        "outcomes": {"alpha": "completed", "beta": "completed", "gamma": "completed"}}
    assert result.kill_matrix("beta") == result.runs["beta"].to_kill_matrix()
    with pytest.raises(InvalidRequest):
        result.kill_matrix("delta")


def test_the_union_and_each_suite_record_where_the_runs_executed(suites):
    """A completion check reads the supervision mode from the saved
    matrices: without an identity none is claimed."""
    from langadapter.outcomes import (
        ExecutionIdentity, FileIdentity, ObservationIdentity, ToolchainIdentity,
    )
    from langadapter.mutation import MutationIdentity

    def identified(run, root):
        return replace(run, identity=MutationIdentity(
            observation=ObservationIdentity(
                target=FileIdentity("target.py", "0" * 64),
                suite=(FileIdentity(f"test_{root}.py", "1" * 64),),
                toolchain=ToolchainIdentity("CPython", "3.12"), instrument=None,
                execution=ExecutionIdentity("local", supervision="process_group")),
            catalogue="full", operators=(), per_mutant_timeout_s=7,
            attribution="first", workers=1))

    alpha, beta, gamma = (suite.suite.root for suite in suites)
    plain = {alpha: run_of(mutant(FIRST, "killed", ("t::a",))),
             beta: run_of(mutant(FIRST, "survived")),
             gamma: run_of(mutant(FIRST, "survived"))}
    assert "execution" not in Session(plain).per_suite(request_for(suites)).kill_matrix()
    table = {root: identified(run, index) for index, (root, run) in enumerate(plain.items())}
    result = Session(table).per_suite(request_for(suites))
    expected = {"kind": "local", "image": None, "supervision": "process_group"}
    assert result.kill_matrix()["execution"] == expected
    assert result.kill_matrix("beta")["execution"] == expected


def test_a_suite_the_session_cannot_run_is_refused_before_any_run(suites):
    table = {suite.suite.root: run_of(mutant(FIRST, "survived")) for suite in suites}
    session = Session(table, invalid=suites[2].suite.root)
    with pytest.raises(InvalidRequest, match="cannot run this suite"):
        session.per_suite(request_for(suites))
    assert session.run_calls == [] and session.close_calls == 0
    assert session.per_suite(request_for(suites[:2])).outcome is Outcome.COMPLETED


def test_a_closed_session_measures_nothing(suites):
    table = {suite.suite.root: run_of(mutant(FIRST, "survived")) for suite in suites}
    session = Session(table)
    session.close()
    result = session.per_suite(request_for(suites))
    assert session.run_calls == []
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE and result.mutants == ()


def test_a_faster_implementation_answers_and_the_reference_stays_reachable(suites):
    table = {suite.suite.root: run_of(mutant(FIRST, "killed", ("t::a",))) for suite in suites}
    session = FastSession(table, table)
    result = session.per_suite(request_for(suites))
    assert result.implementation == "shared_mutant" and session.run_calls == []
    assert all(run.resolution.requested == "measure" for run in result.runs.values())
    # Only the conformance check asks for the reference of such a session.
    reference = session._per_suite(request_for(suites), fast=False)
    assert reference.implementation == DEFAULT_IMPLEMENTATION and len(session.run_calls) == 3


def test_a_failing_faster_implementation_closes_the_session(suites):
    table = {suite.suite.root: run_of(mutant(FIRST, "survived")) for suite in suites}
    session = FastSession(table, table, error=RuntimeError("the shared run broke"))
    with pytest.raises(RuntimeError, match="shared run broke"):
        session.per_suite(request_for(suites))
    assert session.close_calls == 1


def test_conformance_compares_the_faster_implementation_with_the_reference(suites):
    alpha, beta, gamma = (suite.suite.root for suite in suites)
    table = {alpha: run_of(mutant(FIRST, "killed", ("t::a",)), mutant(SECOND, "survived")),
             beta: run_of(mutant(FIRST, "timeout"), mutant(SECOND, "survived")),
             gamma: run_of(mutant(FIRST, "survived"), mutant(SECOND, "survived"))}
    request = request_for(suites)
    assert check_per_suite(FastSession(table, dict(table)), request) == ()
    assert check_per_suite(Session(table), request) == ()
    differing = dict(table, **{beta: run_of(mutant(FIRST, "survived"), mutant(SECOND, "survived")),
                               gamma: run_of(mutant(FIRST, "survived"))})
    problems = check_per_suite(FastSession(table, differing), request)
    assert problems == (
        "per-suite shared_mutant: suite beta, mutant AddToSub at line 3 (occurrence 0): "
        "survived [], the reference timeout []",
        "per-suite shared_mutant: suite gamma, mutant AddToSub at line 3 (occurrence 1): "
        "untested [], the reference survived []",
    )
    failed = dict(table, **{gamma: run_of(outcome=Outcome.TIMED_OUT)})
    assert any("suite gamma ended timed_out, the reference completed" in problem
               for problem in check_per_suite(FastSession(table, failed), request))
