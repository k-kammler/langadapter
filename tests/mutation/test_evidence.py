"""The evidence a kill-matrix row records, read as typed items, and the rules
that decide a mutant's status under a test subset."""
from langadapter import mutation as ev
from langadapter import mutation as km


def test_a_row_without_evidence_reads_as_none():
    assert ev.evidence_of({"id": "m", "status": "killed"}) == ()


def test_a_verification_reads_back_as_recorded():
    verification = ev.Verification(
        verdict=ev.CORRECTED, clock=False,
        recorded=ev.NativeRecord("KILLED", "NORMAL"),
        attempts=(None, ev.NativeRecord("SURVIVED", "NORMAL", reported=True)),
        replaced=True)
    row = {"id": "m", "status": "survived",
           "verification": verification.to_dict()}
    assert ev.evidence_of(row) == (verification,)
    assert row["verification"]["attempts"][0] is None
    assert "output" not in row["verification"]["recorded"]


def test_a_kept_output_is_part_of_the_record():
    record = ev.NativeRecord("KILLED", "NORMAL", output="Segmentation fault")
    assert record.to_dict()["output"] == "Segmentation fault"
    assert ev.NativeRecord.from_dict(record.to_dict()) == record


def test_a_file_attribution_names_the_files_that_kill_alone():
    row = {"id": "m", "status": "killed",
           "killers": ["test_a.py::per_file_adjudication"],
           "adjudication": {"outcome": ev.ATTRIBUTED,
                            "files": {"test_b.py": False, "test_a.py": True,
                                      "test_c.py": None}}}
    (attribution,) = ev.evidence_of(row)
    assert attribution.files == ("test_a.py",)
    assert attribution.verdicts == (("test_a.py", True), ("test_b.py", False),
                                    ("test_c.py", None))
    assert ev.FileAttribution.from_dict(attribution.to_dict()) == attribution


def test_the_timeout_readings_of_the_journal():
    named = {"id": "m", "status": "killed",
             "pre_attribution_status": "timeout",
             "timeout_evidence": ev.NAMED_KILLER,
             "killers": ["t.py::test_a", "t.py::test_b"]}
    hang = {"id": "m", "status": "timeout", "timeout_evidence": ev.HANG,
            "killers": ["t.py::test_slow"], "killer": "t.py::test_slow"}
    cumulative = {"id": "m", "status": "timeout",
                  "timeout_evidence": ev.CUMULATIVE}
    assert ev.evidence_of(named) == (ev.TimeoutEvidence(
        ev.NAMED_KILLER, ("t.py::test_a", "t.py::test_b"), "timeout"),)
    assert ev.evidence_of(hang) == (ev.TimeoutEvidence(
        ev.HANG, ("t.py::test_slow",), "timeout"),)
    assert ev.evidence_of(cumulative) == (ev.TimeoutEvidence(
        ev.CUMULATIVE, (), "timeout"),)


def test_the_methods_read_in_the_order_they_apply():
    row = {"id": "m", "status": "survived",
           "derived": {"measured_status": "killed",
                       "rule": km.RULE_NO_KEPT_KILLER},
           "adjudication": {"outcome": ev.POOL_CONTEXT,
                            "files": {"a.py": False, "b.py": False}},
           "verification": {"verdict": ev.UNRESOLVED, "clock": False,
                            "recorded": {"test_outcome": "KILLED"},
                            "attempts": [None, None], "replaced": False}}
    assert [item.kind for item in ev.evidence_of(row)] == [
        "verification", "file_attribution", "derived"]


def _covers(lines):
    return lambda line: line in lines


def test_each_rule_of_a_derivation():
    cases = [
        ({"id": "a", "status": "survived"}, km.RULE_NOT_KILLED),
        ({"id": "a", "status": "untested"}, km.RULE_NOT_KILLED),
        ({"id": "a", "status": "non_viable"}, km.RULE_NON_VIABLE),
        ({"id": "a", "status": "killed"}, km.RULE_WITHOUT_NAMED_TEST),
        ({"id": "k", "status": "killed", "killers": ["t::x"]},
         km.RULE_KEPT_KILLER),
        ({"id": "a", "status": "killed", "killers": ["t::x"]},
         km.RULE_NO_KEPT_KILLER),
        ({"id": "k", "status": "timeout", "line": 3}, km.RULE_KEPT_KILLER),
        ({"id": "a", "status": "timeout", "line": 10}, km.RULE_COVERED_LINE),
        ({"id": "a", "status": "timeout", "line": 3}, km.RULE_NO_KEPT_KILLER),
        ({"id": "a", "status": "timeout"}, km.RULE_TIMEOUT_WITHOUT_LINE),
        # The clock or a lost run of every test left only the first killers.
        ({"id": "a", "status": "killed", "killers": ["t::x"], "line": 10,
          "pre_attribution_status": "timeout"}, km.RULE_COVERED_LINE),
        ({"id": "a", "status": "killed", "killers": ["t::x"], "line": 3,
          "pre_attribution_status": "timeout"}, km.RULE_NO_KEPT_KILLER),
        ({"id": "a", "status": "killed", "killers": ["t::x"], "line": 10,
          "killers_incomplete": True}, km.RULE_COVERED_LINE),
        ({"id": "a", "status": "killed", "killers": ["t::x"], "line": 3,
          "killers_incomplete": True}, km.RULE_NO_KEPT_KILLER),
    ]
    for row, rule in cases:
        assert km.derivation_rule(row, {"k"}, _covers({10})) == rule, row
        assert km.killed_under(row, {"k"}, _covers({10})) is (
            rule in (km.RULE_NON_VIABLE, km.RULE_WITHOUT_NAMED_TEST, km.RULE_KEPT_KILLER,
                     km.RULE_COVERED_LINE, km.RULE_TIMEOUT_WITHOUT_LINE)), row


def test_a_restricted_row_clears_the_killers_of_an_unreproduced_kill():
    row = {"id": "a", "status": "killed", "killer": "t::x",
           "killers": ["t::x"], "diff": "d"}
    assert km._restricted_row(row, km.RULE_NO_KEPT_KILLER) == {
        "id": "a", "status": "survived", "killer": None, "killers": [],
        "diff": "d"}
    assert km._restricted_row(row, km.RULE_KEPT_KILLER) == row
    assert km._restricted_row(row, km.RULE_KEPT_KILLER) is not row


def test_a_crash_observation_reads_as_what_it_is():
    row = {"id": "m", "status": "killed",
           "active_at_end": "t.py::test_crash"}
    assert ev.evidence_of(row) == (ev.CrashObservation("t.py::test_crash"),)
    assert ev.CrashObservation.kind == "crash_observation"
