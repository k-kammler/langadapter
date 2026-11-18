"""A kill matrix stamped incomplete reports no score, only a lower bound.

The mutants a run leaves unresolved are the slow ones and a timeout counts
as detected, so scoring the resolved subset reads high. The verdicts stay
readable as evidence.
"""
from __future__ import annotations

from langadapter import mutation as km


def _matrix() -> dict:
    return {
        "mutants": [
            {"id": 1, "status": "killed", "operator": "core/AddNot"},
            {"id": 2, "status": "survived", "operator": "core/AddNot"},
            {"id": 3, "status": "timeout", "operator": "core/NumberRep"},
            {"id": 4, "status": "killed", "operator": "core/NumberRep"},
        ]
    }


def test_resolved_matrix_scores_normally() -> None:
    assert km.matrix_score_percent(_matrix()) == 75.0


def test_incomplete_matrix_reports_no_score() -> None:
    m = _matrix()
    m[km.INCOMPLETE_KEY] = {
        "reason": "untested_mutants_in_catalogue", "untested": 7, "total": 11,
    }
    assert km.matrix_score_percent(m) is None


def test_incomplete_gate_holds_for_a_selective_subset() -> None:
    """An incomplete matrix withholds a selective score too."""
    m = _matrix()
    ops = {"core/AddNot"}
    assert km.matrix_score_percent(m, operators=ops) == 50.0
    m[km.INCOMPLETE_KEY] = {"reason": "untested_mutants_in_catalogue"}
    assert km.matrix_score_percent(m, operators=ops) is None


def test_absent_or_falsy_stamp_scores_normally() -> None:
    """A matrix without the stamp, or with a false one, is scored."""
    m = _matrix()
    assert km.matrix_score_percent(m) == 75.0
    m[km.INCOMPLETE_KEY] = None
    assert km.matrix_score_percent(m) == 75.0
    m[km.INCOMPLETE_KEY] = {}
    assert km.matrix_score_percent(m) == 75.0


def test_killed_and_valid_ids_are_unaffected() -> None:
    """An incomplete matrix withholds the score but keeps its verdicts."""
    m = _matrix()
    m[km.INCOMPLETE_KEY] = {"reason": "untested_mutants_in_catalogue"}
    assert km.killed_ids(m) == {1, 3, 4}
    assert km.valid_ids(m) == {1, 2, 3, 4}


def _with_untested() -> dict:
    m = _matrix()
    m["mutants"].append(
        {"id": 5, "status": "untested", "operator": "core/AddNot"})
    return m


def test_an_untested_mutant_counts_as_not_killed() -> None:
    """A row without a result is in the denominator but not killed."""
    m = _with_untested()
    assert km.valid_ids(m) == {1, 2, 3, 4, 5}
    assert km.killed_ids(m) == {1, 3, 4}
    assert km.matrix_score_percent(m) == 60.0
    assert km.matrix_score_percent(m, operators={"core/AddNot"}) == 100.0 / 3


def test_a_stamped_matrix_has_a_lower_bound_but_no_score() -> None:
    m = _with_untested()
    m[km.INCOMPLETE_KEY] = {
        "reason": "untested_mutants_in_catalogue", "untested": 1, "total": 5,
    }
    assert km.matrix_score_percent(m) is None
    assert km.matrix_lower_bound_percent(m) == 60.0
    assert km.matrix_lower_bound_percent(m, operators={"core/AddNot"}) == 100.0 / 3


def test_the_lower_bound_of_an_unstamped_matrix_is_its_score() -> None:
    for m in (_matrix(), _with_untested()):
        assert km.matrix_lower_bound_percent(m) == km.matrix_score_percent(m)
    assert km.matrix_lower_bound_percent({"mutants": []}) is None
    assert km.matrix_lower_bound_percent(None) is None


def test_only_untested_rows_without_a_kill_give_a_bound_of_zero() -> None:
    m = {"mutants": [{"id": 1, "status": "untested"}],
         km.INCOMPLETE_KEY: {"reason": "untested_mutants_in_catalogue"}}
    assert km.matrix_score_percent(m) is None
    assert km.matrix_lower_bound_percent(m) == 0.0


def test_the_stamp_reason_keeps_its_recorded_value() -> None:
    """The reason a matrix with untested mutants is stamped incomplete is one
    constant, with the value Testuary records."""
    assert km.UNTESTED_MUTANTS_IN_CATALOGUE == "untested_mutants_in_catalogue"
