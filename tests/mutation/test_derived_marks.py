"""A derived result says so, with a rule per mutant, in the typed and the
saved form, and every row carries the mutant's key, so derived and measured
verdicts compare mutant by mutant whatever the engine's own ids."""
from __future__ import annotations

import dataclasses

from langadapter import mutation as ev
from langadapter import mutation as km
from langadapter.mutation import BY_FAILING_TEST, Classification, MutationClass
from langadapter.mutation import Mutant, MutationRun, mutant_key

KILLED = Classification("killed", MutationClass.DETECTED, BY_FAILING_TEST)


def _matrix():
    return {"mutants": [
        {"id": 1, "line": 3, "status": "killed", "killers": ["t::a"]},
        {"id": 2, "line": 4, "status": "killed", "killers": ["t::b"]},
        {"id": 3, "line": 5, "status": "timeout", "killers": []},
        {"id": 4, "line": 6, "status": "survived", "killers": []},
    ]}


def test_a_restricted_matrix_is_marked_derived_with_each_mutants_rule():
    out = km.restrict_kill_matrix(_matrix(), kept_killed_ids={1}, kept_lines={5})
    assert out["derived"] == {"kind": "kill_subset", "kept_killed_ids": [1],
                              "covered_lines": [5], "of_derived": False,
                              "assumption": ev.DERIVATION_ASSUMPTION}
    rules = [row["derived"] for row in out["mutants"]]
    assert rules == [
        {"measured_status": "killed", "rule": km.RULE_KEPT_KILLER},
        {"measured_status": "killed", "rule": km.RULE_NO_KEPT_KILLER},
        {"measured_status": "timeout", "rule": km.RULE_COVERED_LINE},
        {"measured_status": "survived", "rule": km.RULE_NOT_KILLED},
    ]
    assert [row["status"] for row in out["mutants"]] == [
        "killed", "survived", "timeout", "survived"]
    assert "resolution" not in out


def test_a_matrix_restricted_twice_keeps_the_measured_status():
    once = km.restrict_kill_matrix(_matrix(), kept_killed_ids={1, 2}, kept_lines=set())
    twice = km.restrict_kill_matrix(once, kept_killed_ids={1}, kept_lines=set())
    assert twice["derived"]["of_derived"] is True
    assert twice["mutants"][1]["derived"] == {"measured_status": "killed",
                                              "rule": km.RULE_NO_KEPT_KILLER}


def test_every_row_of_a_saved_run_carries_the_mutants_key():
    key = mutant_key("calc.py", "core/AddToSub", 2, 7, 11, "-a + b\n+a - b")
    row = {"id": "job-1", "line": 7, "operator": "core/AddToSub", "status": "killed",
           "killers": ["t::a"], "killer": "t::a"}
    run = MutationRun("calc.py", (Mutant(key, "job-1", None, None, KILLED, ("t::a",),
                                         "-a + b\n+a - b", row),), {}, "full", ())
    (saved,) = run.to_kill_matrix()["mutants"]
    assert saved["key"] == dataclasses.asdict(key)
    # Restriction keeps it, so the derived row names the same mutant.
    (derived,) = km.restrict_kill_matrix(run.to_kill_matrix(), kept_killed_ids=set())["mutants"]
    assert derived["key"] == saved["key"] and derived["status"] == "survived"
