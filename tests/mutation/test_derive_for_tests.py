"""A mutation run's result for a subset of its tests, derived without
running them: the rules per mutant, the derived mark with its assumption,
and the cases no rule decides."""
import dataclasses

import pytest

from langadapter.errors import InvalidRequest
from langadapter.outcomes import (
    ExecutionIdentity, FileIdentity, ObservationIdentity, ToolchainIdentity,
)
from langadapter import mutation as ev
from langadapter import mutation as km
from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, NON_VIABLE, SURVIVED, WITHOUT_NAMED_TEST,
    Classification, MutationClass,
)
from langadapter.mutation import (
    Mutant, MutationIdentity, MutationRun, mutant_key,
)
from langadapter.suites import TestId

A = TestId("test_a.py", "test_one", text="test_a.py::test_one")
B = TestId("test_b.py", "test_two", text="test_b.py::test_two")
DETECTED_BY_TEST = Classification("killed", MutationClass.DETECTED,
                                  BY_FAILING_TEST)
TIMEOUT = Classification("timeout", MutationClass.DETECTED, BY_TIMEOUT)


def _mutant(job, row, classification, killing_tests=()):
    row = {"id": job, **row}
    return Mutant(
        key=mutant_key("m.py", "core/Op", 0, row.get("line"), 0, "d" + job),
        engine_id=job, test_outcome=None, worker_outcome=None,
        classification=classification, killers=tuple(row.get("killers", ())),
        diff="d" + job, row=row, killing_tests=tuple(killing_tests))


def _run(*mutants, attribution="all"):
    identity = MutationIdentity(
        observation=ObservationIdentity(
            target=FileIdentity("m.py", "0" * 64),
            suite=(FileIdentity("test_a.py", "1" * 64),
                   FileIdentity("test_b.py", "2" * 64)),
            toolchain=ToolchainIdentity("CPython", "3.12"),
            instrument=None, execution=ExecutionIdentity("local")),
        catalogue="full", operators=(), per_mutant_timeout_s=10,
        attribution=attribution, workers=1)
    rows = [dict(m.row) for m in mutants]
    return MutationRun(target_file="m.py", mutants=tuple(mutants),
                       summary=km._summarize(rows), requested_catalogue="full",
                       requested_operators=(), identity=identity)


def _measured():
    return _run(
        _mutant("by_a", {"line": 2, "status": "killed",
                         "killer": "../x/test_a.py::test_one",
                         "killers": ["../x/test_a.py::test_one"]},
                DETECTED_BY_TEST, [A]),
        _mutant("by_b", {"line": 3, "status": "killed",
                         "killer": "../x/test_b.py::test_two",
                         "killers": ["../x/test_b.py::test_two"]},
                DETECTED_BY_TEST, [B]),
        _mutant("hang_b", {"line": 4, "status": "timeout",
                           "timeout_evidence": ev.HANG,
                           "killer": "../x/test_b.py::test_two",
                           "killers": ["../x/test_b.py::test_two"]},
                TIMEOUT, [B]),
        _mutant("slow", {"line": 5, "status": "timeout"}, TIMEOUT),
        _mutant("crash", {"line": 6, "status": "killed"}, Classification(
            "killed", MutationClass.DETECTED, WITHOUT_NAMED_TEST)),
        _mutant("bad", {"line": 7, "status": "non_viable"}, Classification(
            "non_viable", MutationClass.DETECTED, NON_VIABLE)),
        _mutant("lives", {"line": 8, "status": "survived"}, Classification(
            "survived", MutationClass.UNDETECTED, SURVIVED)),
    )


def test_each_mutant_records_its_measured_status_and_the_rule():
    derived = _measured().derive_for_tests([A], covered_lines=[2, 5])
    got = {m.engine_id: (m.status, m.row["derived"]["rule"])
           for m in derived.mutants}
    assert got == {
        "by_a": ("killed", km.RULE_KEPT_KILLER),
        "by_b": ("survived", km.RULE_NO_KEPT_KILLER),
        "hang_b": ("survived", km.RULE_NO_KEPT_KILLER),
        "slow": ("timeout", km.RULE_COVERED_LINE),
        "crash": ("killed", km.RULE_WITHOUT_NAMED_TEST),
        "bad": ("non_viable", km.RULE_NON_VIABLE),
        "lives": ("survived", km.RULE_NOT_KILLED),
    }
    by_id = {m.engine_id: m for m in derived.mutants}
    unkilled = by_id["by_b"]
    assert unkilled.killers == () and unkilled.killing_tests == ()
    assert unkilled.row["killers"] == [] and unkilled.row["killer"] is None
    assert unkilled.classification == Classification(
        "survived", MutationClass.UNDETECTED, SURVIVED)
    assert unkilled.evidence == (
        ev.DerivedVerdict("killed", km.RULE_NO_KEPT_KILLER),)
    # The journal's reading stays with the row it explains.
    assert [e.kind for e in by_id["hang_b"].evidence] == ["timeout", "derived"]
    assert by_id["by_a"].killing_tests == (A,)


def test_a_kill_that_stays_names_only_the_kept_tests():
    """A mutant both tests killed stays killed for A alone, and its killers,
    in the row and the kill matrix, are A's only."""
    both = _run(_mutant("by_both", {
        "line": 2, "status": "killed", "killer": "../x/test_a.py::test_one",
        "killers": ["../x/test_a.py::test_one", "../x/test_b.py::test_two"]},
        DETECTED_BY_TEST, [A, B]))
    mutant, = both.derive_for_tests([A], covered_lines=[2]).mutants
    assert (mutant.status, mutant.killers, mutant.killing_tests) == (
        "killed", ("../x/test_a.py::test_one",), (A,))
    assert (mutant.row["killer"], mutant.row["killers"]) == (
        "../x/test_a.py::test_one", ["../x/test_a.py::test_one"])
    row, = both.derive_for_tests([A], covered_lines=[2]).to_kill_matrix()["mutants"]
    assert row["killing_tests"] == [dataclasses.asdict(A)]


def test_a_timeout_the_journal_attributes_stays_killed_with_its_test():
    derived = _measured().derive_for_tests([B], covered_lines=[])
    by_id = {m.engine_id: m for m in derived.mutants}
    assert (by_id["hang_b"].status, by_id["hang_b"].row["derived"]["rule"]) \
        == ("timeout", km.RULE_KEPT_KILLER)
    assert (by_id["slow"].status, by_id["slow"].row["derived"]["rule"]) \
        == ("survived", km.RULE_NO_KEPT_KILLER)


def test_the_derived_result_is_marked_and_states_its_assumption():
    measured = _measured()
    derived = measured.derive_for_tests([A], covered_lines=[5, 2])
    assert measured.derivation is None
    assert derived.derivation == ev.Derivation(
        kept_tests=("test_a.py::test_one",), covered_lines=(2, 5),
        attribution="all")
    matrix = derived.to_kill_matrix()
    assert matrix["derived"] == {
        "kind": "test_subset", "kept_tests": ["test_a.py::test_one"],
        "covered_lines": [2, 5], "attribution": "all",
        "assumption": ev.DERIVATION_ASSUMPTION}
    assert "removed tests" in matrix["derived"]["assumption"]
    assert "timeout" in matrix["derived"]["assumption"]
    assert matrix["summary"] == km._summarize(matrix["mutants"])
    assert matrix["summary"]["detected"] == 4 and matrix["summary"]["survived"] == 3
    # Scaled before dividing, as every percentage.
    assert derived.score() == 100.0 * 4 / 7 == 57.142857142857146
    assert measured.score() == pytest.approx(100.0 * 6 / 7)
    assert "derived" not in measured.to_kill_matrix()


def test_the_typed_derivation_agrees_with_the_matrix_derivation():
    measured = _measured()
    derived = measured.derive_for_tests([A], covered_lines=[2, 5])
    restricted = km.restrict_kill_matrix(
        measured.to_kill_matrix(), kept_killed_ids={"by_a"},
        kept_lines={2, 5})
    assert [m.status for m in derived.mutants] == [
        row["status"] for row in restricted["mutants"]]
    assert derived.to_kill_matrix()["summary"] == restricted["summary"]


def test_derivations_no_rule_decides_are_refused():
    by_file = _run(_mutant(
        "adjudicated", {"line": 2, "status": "killed",
                        "killers": ["test_a.py::per_file_adjudication"]},
        DETECTED_BY_TEST))
    with pytest.raises(InvalidRequest) as refused:
        by_file.derive_for_tests([A], covered_lines=[])
    assert refused.value.field == "run"
    assert "test_a.py::per_file_adjudication" in refused.value.message
    with pytest.raises(InvalidRequest) as refused:
        _measured().derive_for_tests(
            [TestId("test_c.py", "test_x")], covered_lines=[])
    assert refused.value.field == "kept"
    unmatched = _run(dataclasses.replace(_mutant(
        "outside", {"line": 2, "status": "killed",
                    "killers": ["../elsewhere/test_c.py::test_x"]},
        DETECTED_BY_TEST), unmatched_killers=(
            "../elsewhere/test_c.py::test_x",)))
    assert unmatched.unmatched_killers() == ("../elsewhere/test_c.py::test_x",)
    with pytest.raises(InvalidRequest) as refused:
        unmatched.derive_for_tests([A], covered_lines=[])
    assert refused.value.field == "run"
    derived = _measured().derive_for_tests([A], covered_lines=[])
    with pytest.raises(InvalidRequest) as refused:
        derived.derive_for_tests([A], covered_lines=[])
    assert refused.value.field == "run"


def test_a_first_killer_run_records_its_attribution_in_the_mark():
    measured = _run(_mutant(
        "by_a", {"line": 2, "status": "killed",
                 "killers": ["test_a.py::test_one"]}, DETECTED_BY_TEST, [A]),
        attribution="first")
    derived = measured.derive_for_tests([B], covered_lines=[])
    assert derived.derivation.attribution == "first"
    assert derived.to_kill_matrix()["derived"]["attribution"] == "first"


@pytest.mark.parametrize("attribution", ["first", "all"])
def test_a_runs_matrix_records_how_it_recorded_killing_tests(attribution):
    """A stored matrix names its run's attribution, so a derivation from it
    knows whether every killing test is in it."""
    assert _run(attribution=attribution).to_kill_matrix()["attribution"] == attribution


def test_a_runs_matrix_names_each_kills_recorded_tests_until_a_derivation_drops_it():
    """A row with killers names the tests the run recorded as its killers
    and the killers it placed in none. A derived row whose kill the kept
    tests do not reproduce names none, typed or from the stored matrix."""
    measured = _run(
        _mutant("by_a", {"line": 2, "status": "killed", "killers": ["../x/test_a.py::test_one"]},
                DETECTED_BY_TEST, [A]),
        _mutant("by_b", {"line": 3, "status": "killed", "killers": ["../x/test_b.py::test_two"]},
                DETECTED_BY_TEST, [B]),
        _mutant("lives", {"line": 4, "status": "survived"}, Classification(
            "survived", MutationClass.UNDETECTED, SURVIVED)))
    rows = {row["id"]: row for row in measured.to_kill_matrix()["mutants"]}
    assert rows["by_a"]["killing_tests"] == [dataclasses.asdict(A)]
    assert "unmatched_killers" not in rows["by_a"]
    assert "killing_tests" not in rows["lives"]
    (unplaced,) = _run(dataclasses.replace(_mutant(
        "by_c", {"line": 3, "status": "killed",
                 "killers": ["../x/test_b.py::test_two", "elsewhere.py::test_x"]},
        DETECTED_BY_TEST, [B]), unmatched_killers=("elsewhere.py::test_x",))).to_kill_matrix()["mutants"]
    assert unplaced["killing_tests"] == [dataclasses.asdict(B)]
    assert unplaced["unmatched_killers"] == ["elsewhere.py::test_x"]
    derived = {row["id"]: row for row in measured.derive_for_tests(
        [A], covered_lines=[]).to_kill_matrix()["mutants"]}
    assert derived["by_a"]["killing_tests"] == [dataclasses.asdict(A)]
    assert "killing_tests" not in derived["by_b"] and "unmatched_killers" not in derived["by_b"]
    restricted = {row["id"]: row for row in km.restrict_kill_matrix(
        measured.to_kill_matrix(), kept_killed_ids={"by_a"}, kept_lines=set())["mutants"]}
    assert restricted["by_a"]["killing_tests"] == [dataclasses.asdict(A)]
    assert "killing_tests" not in restricted["by_b"] and "unmatched_killers" not in restricted["by_b"]
