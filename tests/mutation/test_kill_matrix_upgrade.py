"""A stored kill matrix of an earlier schema version reads as version 3:
``upgrade_kill_matrix`` maps its words, and every reader of the module reads
through it. "detected" names one set of mutants everywhere."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from langadapter import mutation as km
from langadapter.c import _mutation as c_mutation
from langadapter.errors import InvalidRequest
from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, NON_VIABLE, SURVIVED, UNTESTED, Catalogue, Classification,
    MutationClass, MutationRun, Mutant, mutant_key,
)
from langadapter.suites import Suite

#: A version-2 matrix with every earlier word: each status, a timeout the
#: journal attributed, a derived row of the rule "invalid" and a per-suite
#: verdict "not_measured".
VERSION_2 = {
    "schema_version": 2,
    "target_file": "calc.py",
    "mutants": [
        {"id": "k", "line": 1, "status": "ok_killed", "killers": ["t.py::a"]},
        {"id": "a", "line": 2, "status": "ok_killed", "killers": ["t.py::b"],
         "pre_attribution_status": "bad_timeout"},
        {"id": "t", "line": 3, "status": "bad_timeout", "killers": []},
        {"id": "n", "line": 4, "status": "ok_suspicious", "killers": [],
         "derived": {"measured_status": "ok_suspicious", "rule": "invalid"}},
        {"id": "s", "line": 5, "status": "bad_survived", "killers": [],
         "suite_verdicts": {"one": {"verdict": "not_measured", "killers": []},
                            "two": {"verdict": "survived", "killers": []}}},
        {"id": "u", "line": 6, "status": "untested", "killers": []},
    ],
    "summary": {"total": 6, "killed": 4, "survived": 1, "timeout": 1,
                "suspicious": 1, "untested": 1, "unresolved": 0},
}


def test_a_version_2_matrix_reads_in_the_version_3_words():
    stored = copy.deepcopy(VERSION_2)
    upgraded = km.upgrade_kill_matrix(stored)

    assert stored == VERSION_2
    assert upgraded["schema_version"] == km.KILL_MATRIX_SCHEMA_VERSION == 3
    rows = {row["id"]: row for row in upgraded["mutants"]}
    assert [row["status"] for row in upgraded["mutants"]] == [
        "killed", "killed", "timeout", "non_viable", "survived", "untested"]
    assert rows["a"]["pre_attribution_status"] == "timeout"
    assert rows["n"]["derived"] == {"measured_status": "non_viable", "rule": "non_viable"}
    assert rows["s"]["suite_verdicts"]["one"]["verdict"] == "untested"
    assert rows["s"]["suite_verdicts"]["two"]["verdict"] == "survived"
    assert upgraded["summary"] == {"total": 6, "detected": 4, "survived": 1, "timeout": 1,
                                   "non_viable": 1, "untested": 1, "unresolved": 0}
    assert upgraded["target_file"] == "calc.py"
    assert "pre_attribution_status" not in rows["k"]


def test_a_matrix_without_a_version_is_upgraded_and_one_of_version_3_copied():
    unversioned = copy.deepcopy(VERSION_2)
    del unversioned["schema_version"]
    assert km.upgrade_kill_matrix(unversioned) == km.upgrade_kill_matrix(VERSION_2)

    current = km.upgrade_kill_matrix(VERSION_2)
    again = km.upgrade_kill_matrix(current)
    assert again == current and again is not current
    assert again["mutants"][0] is not current["mutants"][0]


@pytest.mark.parametrize("version", [4, True, "2"])
def test_a_later_or_unreadable_version_is_refused(version):
    with pytest.raises(InvalidRequest, match="schema_version"):
        km.upgrade_kill_matrix({"schema_version": version, "mutants": []})


def test_every_reader_reads_a_version_2_matrix_as_its_upgrade():
    current = km.upgrade_kill_matrix(VERSION_2)
    for stored in (VERSION_2, current):
        assert km.killed_ids(stored) == {"k", "a", "t", "n"}
        assert km.valid_ids(stored) == {"k", "a", "t", "n", "s", "u"}
        assert km.matrix_score_percent(stored) == pytest.approx(400 / 6)
        assert km.matrix_lower_bound_percent(stored) == pytest.approx(400 / 6)
        assert km.timeout_kill_count(stored) == 1
        assert km.unattributed_kill_fraction(stored) == 0.0
        assert km.survivors_from_matrix(stored) == [{"id": "s", "line": 5, "diff": None}]
        assert km.has_operators(stored) is False
        restricted = km.restrict_kill_matrix(stored, kept_killed_ids={"k"}, kept_lines={3})
        assert restricted["schema_version"] == 3
        assert {row["id"]: row["status"] for row in restricted["mutants"]} == {
            "k": "killed", "a": "survived", "t": "timeout", "n": "non_viable",
            "s": "survived", "u": "untested"}
        assert restricted["summary"]["detected"] == 3


def test_refresh_summary_upgrades_a_version_2_matrix_in_place():
    stored = copy.deepcopy(VERSION_2)
    refreshed = km.refresh_summary(stored)

    assert refreshed is stored
    assert stored == {**km.upgrade_kill_matrix(VERSION_2), "summary": stored["summary"]}
    assert stored["summary"] == {"total": 6, "detected": 4, "survived": 1, "timeout": 1,
                                 "non_viable": 1, "untested": 1, "unresolved": 0}


def _run(*classifications):
    mutants = tuple(
        Mutant(key=mutant_key("calc.py", "op", index, index + 1, 0, f"-{index}"),
               engine_id=str(index), test_outcome=None, worker_outcome=None,
               classification=classification, killers=(), diff=f"-{index}",
               row={"id": str(index), "line": index + 1, "status": classification.status})
        for index, classification in enumerate(classifications))
    return MutationRun("calc.py", mutants, km._summarize([dict(m.row) for m in mutants]),
                       "full", ())


def test_detected_counts_the_class_detected():
    """On a run with every status, the summary's "detected" is the number of
    mutants of the class DETECTED, the non-viable mutant among them."""
    run = _run(Classification("killed", MutationClass.DETECTED, BY_FAILING_TEST),
               Classification("timeout", MutationClass.DETECTED, BY_TIMEOUT),
               Classification("non_viable", MutationClass.DETECTED, NON_VIABLE),
               Classification("survived", MutationClass.UNDETECTED, SURVIVED),
               Classification("untested", MutationClass.NOT_RUN, UNTESTED))

    matrix = run.to_kill_matrix()
    assert matrix["summary"]["detected"] == run.counts()[MutationClass.DETECTED] == 3
    assert matrix["summary"]["non_viable"] == 1
    assert set(run.counts()) == {MutationClass.DETECTED, MutationClass.UNDETECTED,
                                 MutationClass.NOT_RUN}
    assert km.matrix_score_percent(matrix) == run.score() == 60.0


def test_a_non_viable_mutant_takes_its_own_subcategory_and_verdict():
    non_viable = Classification("non_viable", MutationClass.DETECTED, NON_VIABLE)
    run = _run(non_viable)

    assert km._verdict_of(run.mutants[0]) == km.SuiteVerdict("non_viable")
    assert km.RULE_NON_VIABLE == "non_viable"
    assert km.derivation_rule(dict(run.mutants[0].row), set(), lambda line: False) == (
        km.RULE_NON_VIABLE)
    assert km.killed_under(dict(run.mutants[0].row), set(), lambda line: False)


#: The C source of the two Mull mutants below.
C_SOURCE = (b"int ready(int made) {\n    return made == 0;\n}\n"
            b"int add(int a, int b) {\n    return a + b;\n}\n")

#: Version-2 matrices of Cosmic Ray and PIT, each with one mutant killed
#: without a named test: a worker that ended abnormally, PIT's RUN_ERROR.
OTHER_ENGINES = [
    {"schema_version": 2, "target_file": target, "mutants": [
        {"id": "1", "line": 2, "operator": operator, "status": "ok_killed", **native,
         "killer": None, "killers": []}],
     "summary": {"total": 1, "killed": 1, "survived": 0, "timeout": 0, "suspicious": 0,
                 "untested": 0, "unresolved": 0}}
    for target, operator, native in (
        ("calc.py", "core/NumberReplacer", {"worker_outcome": "ABNORMAL"}),
        ("src/main/java/Calc.java", "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
         {"native_status": "RUN_ERROR"}))]


def _c_matrix(tmp_path):
    """A kill matrix as the Mull engine writes it for Python tests: one mutant
    under which the extension did not load, one whose pytest process crashed
    before a test failed."""
    unloaded = c_mutation._MullMutant("m0", "cxx_eq_to_ne", "!=", 2, 17, 2, 19, 0)
    crashed = c_mutation._MullMutant("m1", "cxx_add_to_sub", "-", 5, 14, 5, 15, 0)
    run = SimpleNamespace(failing_tests=lambda: [], tests=())
    report = SimpleNamespace(loaded=lambda extension: False,
                             problem=lambda module, extension: f"{module} did not load")
    harness = SimpleNamespace(run=lambda request, **switches: (run, report))
    measurement = SimpleNamespace(
        adapter=SimpleNamespace(test_id_text=str), build=SimpleNamespace(module="_calc"),
        request=SimpleNamespace(suite=Suite(str(tmp_path), ("test_calc.py",)),
                                catalogue=Catalogue()),
        attribution="first", timeout_s=10, deadline=time.monotonic() + 60, data=C_SOURCE,
        # The timeout journal of an extension's Python tests, on by default
        # (settings). The row this test reads does not record it.
        journal=True,
        # The runs tried once more, the mutants left untested after the
        # process layer failed and those whose per-mutant time ended between
        # two tests, which the measurement always holds.
        retried=[], unsupervised=[], unfinished=[],
        target="src/calc.c", selected=("cxx_eq_to_ne", "cxx_add_to_sub"),
        origin=SimpleNamespace(mutants=(unloaded, crashed), extension=tmp_path / "_calc.so",
                               run_s=1.0))
    verdicts = {0: c_mutation._extension_verdict(measurement, unloaded, harness),
                1: c_mutation._Crashes().verdict(False)}
    return c_mutation._mutation_run(measurement, verdicts, None, {}, 60).to_kill_matrix()


def _as_version_2(matrix):
    """``matrix`` as the Mull engine wrote it at schema version 2, where both
    of its mutants were ok_killed."""
    stored = copy.deepcopy(matrix)
    stored["schema_version"] = 2
    for row in stored["mutants"]:
        row["status"] = "ok_killed"
    summary = {key: value for key, value in matrix["summary"].items()
               if key not in ("detected", "non_viable")}
    stored["summary"] = {**summary, "killed": matrix["summary"]["detected"], "suspicious": 0}
    return stored


def test_a_c_mutant_whose_extension_did_not_load_reads_as_non_viable(tmp_path):
    """Version 2 recorded a mutant under which Mull's extension did not load
    as killed. Its upgrade is what the Mull engine writes now: non_viable,
    still detected, with the summary's counts, also in a derived row. A
    crashed C mutant and Cosmic Ray's and PIT's kills without a named test
    stay killed."""
    written = _c_matrix(tmp_path)
    assert [(row["native_status"], row["status"]) for row in written["mutants"]] == [
        ("load_error", "non_viable"), ("crashed", "killed")]
    stored = _as_version_2(written)
    assert km.upgrade_kill_matrix(stored) == written
    assert km.refresh_summary(copy.deepcopy(stored)) == km.refresh_summary(copy.deepcopy(written))
    derived = km.restrict_kill_matrix(written, kept_killed_ids=set())
    stored_derived = _as_version_2(derived)
    for row in stored_derived["mutants"]:
        row["derived"] = {"measured_status": "ok_killed", "rule": "without_named_test"}
    assert km.upgrade_kill_matrix(stored_derived) == derived
    assert km.killed_ids(stored) == km.killed_ids(written) == {"cxx_eq_to_ne:2:17:2:19",
                                                               "cxx_add_to_sub:5:14:5:15"}
    for other in OTHER_ENGINES:
        upgraded = km.upgrade_kill_matrix(other)
        assert [row["status"] for row in upgraded["mutants"]] == ["killed"]
        assert upgraded["summary"] == {"total": 1, "detected": 1, "survived": 0, "timeout": 0,
                                       "non_viable": 0, "untested": 0, "unresolved": 0}
