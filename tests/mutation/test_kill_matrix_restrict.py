"""Restricting a kill matrix to a subset of tests gives a reduced suite's
score from one measured run.

The subset is given by the mutant ids its ``K:<mutant_id>`` tokens name and
the lines it covers, both opaque, so no engine-specific id is parsed."""
from __future__ import annotations

from langadapter import mutation as km


def _matrix(rows: dict) -> dict:
    """Build a kill matrix from {id: (status, killers, line)}."""
    return {
        "mutants": [
            {"id": i, "status": s, "killers": list(ks), "line": ln}
            for i, (s, ks, ln) in rows.items()
        ],
        "summary": {},
    }


def _by_id(matrix: dict) -> dict:
    return {m["id"]: m for m in matrix["mutants"]}


def _covers(s):
    return lambda ln: ln in s


# --- killed_under: the per-mutant predicate -------------------------------


def test_killed_under_assertion_needs_the_mutant_in_kept_killed_ids() -> None:
    # An assertion kill counts when the mutant id is in the kept killed set,
    # never by parsing the killer string.
    m = {"id": "m1", "status": "killed", "killers": ["t.py::a"], "line": None}
    assert km.killed_under(m, {"m1"}, _covers(set())) is True
    assert km.killed_under(m, set(), _covers(set())) is False


def test_killed_under_single_killer_field_is_an_assertion_kill() -> None:
    # A first-killer run records ``killer`` (singular), also an assertion kill.
    m = {"id": "m1", "status": "killed", "killer": "t.py::a", "line": None}
    assert km.killed_under(m, {"m1"}, _covers(set())) is True
    assert km.killed_under(m, set(), _covers(set())) is False


def test_killed_under_incompetent_and_killed_by_all_need_no_killer() -> None:
    incompetent = {"id": "m1", "status": "non_viable", "killers": [], "line": None}
    killed_by_all = {"id": "m2", "status": "killed", "killers": [], "line": None}
    # Not keyed on a named killer, so an empty killed set still kills them.
    assert km.killed_under(incompetent, set(), _covers(set())) is True
    assert km.killed_under(killed_by_all, set(), _covers(set())) is True


def test_killed_under_timeout_needs_a_covered_line() -> None:
    m = {"id": "m1", "status": "timeout", "killers": [], "line": 10}
    assert km.killed_under(m, set(), _covers({10})) is True
    assert km.killed_under(m, set(), _covers({9})) is False
    # A line-less timeout is killed-by-all (no line to cover).
    m_noline = {"id": "m2", "status": "timeout", "killers": [], "line": None}
    assert km.killed_under(m_noline, set(), _covers(set())) is True


def test_killed_under_attributed_timeout_prefers_the_kept_killer() -> None:
    # A kept named killer keeps the kill without the mutated line, otherwise
    # the line proxy decides.
    m = {"id": "m1", "status": "timeout", "killers": ["t.py::hang"],
         "line": 10}
    assert km.killed_under(m, {"m1"}, _covers(set())) is True
    assert km.killed_under(m, set(), _covers({10})) is True
    assert km.killed_under(m, set(), _covers({9})) is False


def test_killed_under_survived_is_never_killed_by_a_subset() -> None:
    m = {"id": "m1", "status": "survived", "killers": [], "line": None}
    assert km.killed_under(m, {"m1"}, _covers({1, 2})) is False


# --- restrict_kill_matrix: the matrix-level operation ----------------------


def test_restrict_keeping_all_killed_ids_preserves_the_score() -> None:
    m = _matrix({
        1: ("killed", ["t.py::a"], None),
        2: ("killed", ["t.py::b"], None),
        3: ("survived", [], None),
    })
    assert km.matrix_score_percent(m) == 100.0 * 2 / 3
    r = km.restrict_kill_matrix(m, kept_killed_ids={1, 2})
    assert km.matrix_score_percent(r) == 100.0 * 2 / 3
    assert r["summary"]["detected"] == 2
    assert r["summary"]["survived"] == 1
    assert r["summary"]["total"] == 3


def test_restrict_dropping_a_killed_id_unkills_but_keeps_the_denominator() -> None:
    m = _matrix({
        1: ("killed", ["t.py::a"], None),
        2: ("killed", ["t.py::b"], None),
        3: ("survived", [], None),
    })
    r = km.restrict_kill_matrix(m, kept_killed_ids={1})  # mutant 2 no longer killed
    # Mutant 2 survives, and the denominator stays 3.
    assert km.valid_ids(r) == {1, 2, 3}
    byid = _by_id(r)
    assert byid[2]["status"] == km.SURVIVED_STATUS
    assert byid[2]["killers"] == []                  # un-killed -> killers cleared
    assert byid[1]["killers"] == ["t.py::a"]         # still killed -> raw killer kept
    assert km.matrix_score_percent(r) == 100.0 * 1 / 3


def test_restrict_is_monotonic_smaller_subset_scores_no_higher() -> None:
    m = _matrix({
        1: ("killed", ["t.py::a"], None),
        2: ("killed", ["t.py::b"], None),
        3: ("killed", ["t.py::c"], None),
        4: ("survived", [], None),
    })
    full = km.matrix_score_percent(m)
    big = km.matrix_score_percent(km.restrict_kill_matrix(m, kept_killed_ids={1, 2, 3}))
    small = km.matrix_score_percent(km.restrict_kill_matrix(m, kept_killed_ids={1, 3}))
    assert full == 100.0 * 3 / 4
    assert big == full
    assert small == 100.0 * 2 / 4
    assert small <= big <= full


def test_restrict_handles_every_kill_kind_at_once() -> None:
    m = _matrix({
        1: ("killed", ["t.py::a"], None),  # assertion
        2: ("non_viable", [], None),        # incompetent
        3: ("killed", [], None),            # killed-by-all (no killer)
        4: ("timeout", [], 10),            # timeout at line 10
        5: ("survived", [], None),
    })
    # Empty subset: the assertion and timeout kills go, the other two stay.
    r = km.restrict_kill_matrix(m, kept_killed_ids=set(), kept_lines=set())
    byid = _by_id(r)
    assert byid[1]["status"] == km.SURVIVED_STATUS
    assert byid[2]["status"] == "non_viable"
    assert byid[3]["status"] == "killed"
    assert byid[4]["status"] == km.SURVIVED_STATUS
    assert byid[5]["status"] == "survived"
    assert km.matrix_score_percent(r) == 100.0 * 2 / 5
    # Keeping mutant 1 and line 10 brings those two back.
    r2 = km.restrict_kill_matrix(m, kept_killed_ids={1}, kept_lines={10})
    assert km.matrix_score_percent(r2) == 100.0 * 4 / 5


def test_restrict_timeout_line_proxy_can_overcount_a_re_measure() -> None:
    """A timeout kill without a killer stays killed whenever the subset
    covers the mutated line, even when the test that hung is removed, so the
    derived score can exceed a re-measure. mutation_method='both' re-measures
    to show this one-sided inflation."""
    m = _matrix({
        1: ("killed", ["t.py::fast"], 10),   # a fast test covering line 10
        2: ("timeout", [], 10),             # hung on a removed slow test
    })
    r = km.restrict_kill_matrix(m, kept_killed_ids={1}, kept_lines={10})
    byid = _by_id(r)
    assert byid[2]["status"] == "timeout"        # kept killed via the line proxy
    # A re-measure of the fast test alone would survive mutant 2.
    assert km.matrix_score_percent(r) == 100.0 * 2 / 2


def test_restrict_attributed_timeout_survives_on_killer_kept() -> None:
    # A timeout with its hanging test as killer is kept when the subset keeps
    # the killer or covers the mutated line.
    m = _matrix({
        1: ("timeout", ["t.py::hang"], 10),
    })
    kept = km.restrict_kill_matrix(m, kept_killed_ids={1}, kept_lines=set())
    assert _by_id(kept)[1]["status"] == "timeout"
    dropped = km.restrict_kill_matrix(m, kept_killed_ids=set(),
                                      kept_lines=set())
    assert _by_id(dropped)[1]["status"] == km.SURVIVED_STATUS


def test_restrict_recomputes_the_timeout_and_non_viable_summary_counts() -> None:
    m = _matrix({
        1: ("timeout", [], 10),
        2: ("non_viable", [], None),
        3: ("survived", [], None),
    })
    r = km.restrict_kill_matrix(m, kept_killed_ids=set(), kept_lines=set())
    # The uncovered timeout survives, the non-viable mutant stays.
    assert r["summary"] == {
        "total": 3, "detected": 1, "survived": 2,
        "timeout": 0, "non_viable": 1, "untested": 0, "unresolved": 0,
    }


def test_refresh_summary_after_in_place_status_edit() -> None:
    matrix = _matrix({
        1: ("timeout", [], 10),
        2: ("survived", [], None),
    })
    matrix["mutants"][0]["status"] = "killed"
    matrix["mutants"][0]["unresolved"] = True

    assert km.refresh_summary(matrix)["summary"] == {
        "total": 2,
        "detected": 1,
        "survived": 1,
        "timeout": 0,
        "non_viable": 0,
        "untested": 0,
        "unresolved": 1,
    }


def test_restrict_of_none_or_empty_is_empty_and_scores_none() -> None:
    r = km.restrict_kill_matrix(None, kept_killed_ids={"x"})
    assert r["mutants"] == []
    assert km.matrix_score_percent(r) is None
    r2 = km.restrict_kill_matrix({"mutants": [], "summary": {}}, kept_killed_ids=set())
    assert km.matrix_score_percent(r2) is None
