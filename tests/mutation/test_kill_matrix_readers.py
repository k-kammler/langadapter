"""The readers of a stored kill matrix: whether its rows carry operators, its
survivors, and the counts of kills without a named killer, kills by the
clock and unresolved kills, read without a mutation run."""
import pytest

from langadapter.mutation import (
    has_operators, killed_ids, matrix_score_percent, survivors_from_matrix,
    timeout_kill_count, unattributed_kill_fraction, unresolved_kill_count,
)


def matrix(*rows):
    return {"mutants": list(rows), "summary": {}}


def row(mutant_id, status, killers=(), line=None, **fields):
    return {"id": mutant_id, "line": line, "status": status, "killers": list(killers), **fields}


def unattributed(count):
    return [row(f"n{index}", "killed") for index in range(count)]


def attributed(count):
    return [row(f"a{index}", "killed", ("t.py::x",)) for index in range(count)]


def test_operators_are_found_on_any_row():
    assert has_operators(matrix(row(1, "killed", operator="OpA")))
    assert not has_operators(matrix(row(1, "killed")))
    assert not has_operators(None)


def test_a_selective_filter_on_rows_without_an_operator_gives_nothing():
    """Without operator labels a matrix has no selective subset: no score and
    no killed ids, so a caller checks has_operators first."""
    stored = matrix(row(1, "killed", ("t.py::x",)), row(2, "survived"))
    assert matrix_score_percent(stored, operators={"OpA"}) is None
    assert killed_ids(stored, operators={"OpA"}) == set()


def test_the_survivors_come_in_matrix_order_with_their_diffs():
    """A selective catalogue gives its own survivors, and no matrix none."""
    stored = matrix(row(1, "survived", line=10, operator="OpA", diff="d1"),
                    row(2, "killed", line=20, operator="OpA", diff="d2"),
                    row(3, "survived", line=30, operator="OpB", diff="d3"))
    assert survivors_from_matrix(stored) == [{"id": 1, "line": 10, "diff": "d1"},
                                             {"id": 3, "line": 30, "diff": "d3"}]
    assert survivors_from_matrix(stored, operators={"OpA"}) == [{"id": 1, "line": 10, "diff": "d1"}]
    assert survivors_from_matrix(None) == []


@pytest.mark.parametrize("rows, fraction", [
    ([row("s1", "survived"), row("s2", "survived")], None),
    (unattributed(6) + attributed(4) + [row("s1", "survived")], 0.6),
    (attributed(10), 0.0),
    # Timeouts and non_viable mutants are killed but never unattributed.
    (unattributed(6) + [row(f"t{index}", "timeout") for index in range(2)]
     + [row(f"x{index}", "non_viable") for index in range(2)], 0.6),
])
def test_the_unattributed_fraction_counts_kills_without_a_named_killer(rows, fraction):
    assert unattributed_kill_fraction(matrix(*rows)) == fraction


def test_the_clock_and_unresolved_kills_are_counted():
    stored = matrix(*attributed(9), row("t0", "timeout"),
                    row("u0", "killed", unresolved=True), row("u1", "killed", unresolved=False))
    assert timeout_kill_count(stored) == 1
    assert unresolved_kill_count(stored) == 1
    clean = matrix(*attributed(5), row("s1", "survived"))
    assert timeout_kill_count(clean) == unresolved_kill_count(clean) == 0
