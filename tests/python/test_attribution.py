"""Named stored kills are attributed only to uniquely matched tests, from
the killing tests each row records (``LangAdapter.kill_tokens`` and
``pool_kills``), with Python's crash observations."""
import copy

import pytest

import langadapter as lb
from langadapter.python.adapter import PythonAdapter


@pytest.fixture
def python(tmp_path):
    return PythonAdapter(lb.ProjectContext(str(tmp_path)))


def _test(file, qualname, params=None):
    return {"file": file, "qualname": qualname, "params": params, "text": ""}


@pytest.mark.parametrize(
    ("killing", "test_id"),
    [
        (_test("test_seed_003.py", "test_same"), "test_candidate.test_same"),
        (_test("pool/test_seed_003.py", "test_same", "a::b"), "pkg.test_candidate.test_same"),
        (_test("pool/test_seed_003.py", "TestCase.test_same", "2"),
         "test_candidate.TestCase.test_same"),
    ],
)
def test_matches_only_within_the_owning_suite(python, killing, test_id):
    matrix = {"mutants": [{"id": "m", "status": "killed", "killers": ["k"],
                           "killing_tests": [killing]}]}
    before = copy.deepcopy(matrix)
    ids = {0: {test_id}, 1: {test_id}}
    assert python.pool_kills(
        matrix, {"test_seed_003.py": 1, "test_seed_001.py": 0}, ids,
    ) == {1: {test_id: {"K:m"}}}
    assert matrix == before
    assert ids == {0: {test_id}, 1: {test_id}}


def test_keeps_all_killing_tests_and_deduplicates(python):
    matrix = {"mutants": [
        {"id": "m", "killers": ["k1", "k2", "k3"], "killing_tests": [
            _test("test_seed_001.py", "test_a"), _test("test_seed_001.py", "test_a", "2"),
            _test("test_seed_002.py", "test_b")]},
        {"id": "n", "killers": ["k1"], "killing_tests": [_test("test_seed_001.py", "test_a")]},
    ]}
    assert python.pool_kills(
        matrix, {"test_seed_001.py": 0, "test_seed_002.py": 1},
        {0: {"test_candidate.test_a"}, 1: {"test_candidate.test_b"}},
    ) == {
        0: {"test_candidate.test_a": {"K:m", "K:n"}},
        1: {"test_candidate.test_b": {"K:m"}},
    }


def test_does_not_invent_attribution(python):
    matrix = {"mutants": [
        {"id": "collection", "status": "killed"},
        {"id": "timeout", "status": "timeout"},
        {"id": "stored_before", "killer": "test_seed_001.py::test_known"},
        {"id": "unknown_file", "killers": ["k"], "killing_tests": [_test("other.py", "test_a")]},
        {"id": "unknown_test", "killers": ["k"],
         "killing_tests": [_test("test_seed_001.py", "test_missing")]},
        {"id": "no_measurement", "killers": ["k"],
         "killing_tests": [_test("test_seed_002.py", "test_a")]},
        {"id": "ambiguous", "killers": ["k"], "killing_tests": [_test("test_seed_001.py", "test_a")]},
        {"killers": ["k"], "killing_tests": [_test("test_seed_001.py", "test_known")]},
    ]}
    assert python.pool_kills(
        matrix, {"test_seed_001.py": 0, "test_seed_002.py": 1},
        {0: {"a.test_a", "b.test_a", "test_candidate.test_known"}},
    ) == {}


def test_exact_qualified_match_precedes_suffix_matches(python):
    assert python.pool_kills(
        {"mutants": [{"id": 0, "killers": ["k"],
                      "killing_tests": [_test("test_seed.py", "TestCase.test_a")]}]},
        {"test_seed.py": 0}, {0: {"TestCase.test_a", "pkg.TestCase.test_a"}},
    ) == {0: {"TestCase.test_a": {"K:0"}}}


def test_stored_matrix_distinguishes_named_kills_and_crash_observations(python):
    matrix = {"mutants": [
        {"id": "named", "killers": ["k1", "k2"], "killing_tests": [
            _test("test_x.py", "TestX.test_a", "1"), _test("test_x.py", "TestX.test_a", "2")]},
        {"id": "crash", "active_at_end": "test_x.py::TestX::test_a"},
        {"id": "unplaced_crash", "active_at_end": "elsewhere.py::test_x"},
        {"id": "unnamed"},
    ]}
    before = copy.deepcopy(matrix)
    found, unmatched = python.kill_tokens(matrix, {"test_x.TestX.test_a"})
    assert found == {"test_x.TestX.test_a": frozenset({"K:named", "C:crash"})}
    assert unmatched == () and matrix == before


def test_stored_matrix_reports_every_unplaced_named_killer(python):
    known = {"pkg.test_x.test_a", "other.test_x.test_a"}
    found, unmatched = python.kill_tokens({"mutants": [
        {"id": "ambiguous", "killers": ["k"], "killing_tests": [_test("test_x.py", "test_a")]},
        {"id": "missing", "killers": ["k"], "killing_tests": [_test("test_x.py", "test_missing")]},
        {"id": "unplaced", "killers": ["k", "elsewhere.py::test_y"],
         "killing_tests": [], "unmatched_killers": ["elsewhere.py::test_y"]},
    ]}, known)
    assert found == {}
    assert unmatched == ("test_x.py::test_a", "test_x.py::test_missing", "elsewhere.py::test_y")
