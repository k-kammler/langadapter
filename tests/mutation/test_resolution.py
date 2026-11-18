"""A run's resolution records that it measured, and never changes verdicts."""
from __future__ import annotations

import dataclasses
import json

import pytest

from langadapter.errors import InvalidRequest
from langadapter.outcomes import (
    ExecutionIdentity, FileIdentity, InstrumentIdentity, ObservationIdentity,
    ToolchainIdentity,
)
from langadapter import mutation as km
from langadapter.mutation import DERIVATION_ASSUMPTION
from langadapter.mutation import (
    BY_FAILING_TEST, Classification, MutationClass,
)
from langadapter.capabilities import OPERATION_NAMES
from langadapter.mutation import MutationResolution, resolution_of
from langadapter.mutation import Mutant, MutationIdentity, MutationRun, mutant_key
from langadapter.outcomes import Outcome
from langadapter.suites import TestId


def _identity() -> MutationIdentity:
    return MutationIdentity(
        ObservationIdentity(
            FileIdentity("src/target.py", "a" * 64),
            (FileIdentity("test_target.py", "b" * 64),),
            ToolchainIdentity("python", "3.10.14", (("cosmic-ray", "8.4.1"),),
                              "tree-sitter-python"),
            InstrumentIdentity("restore", "1"),
            ExecutionIdentity("container", "benchmark:py310")),
        "operators", ("replace_true", "replace_false"), 10, "all", 2)


def _run() -> MutationRun:
    test = TestId("test_target.py", "test_value")
    mutant = Mutant(
        mutant_key("src/target.py", "replace_true", 0, 1, 1, "-True\n+False"),
        "1", "KILLED", "NORMAL",
        Classification("killed", MutationClass.DETECTED, BY_FAILING_TEST),
        (str(test),), "-True\n+False",
        {"id": "1", "status": "killed", "operator": "replace_true",
         "killers": [str(test)], "line": 1},
        (test,))
    return MutationRun("src/target.py", (mutant,), {"total": 1, "detected": 1},
                       "operators", _identity().operators, identity=_identity())


#: A run's resolution record, byte for byte.
RUN_RESOLUTION = ('{"requested": "measure", "method": "measured", "prior_identity": null, '
                  '"conditions": [], "reason": "native measurement requested"}')


def test_a_session_measures_and_derives_nothing():
    """A session's one way to a result is to measure it: it has no
    derivation from a prior run, and no report names one."""
    assert not hasattr(km.MutationSession, "derive")
    assert "mutations.derive" not in OPERATION_NAMES
    with pytest.raises(InvalidRequest, match="must be measure"):
        MutationResolution("derive", "measured")
    for method in ("reused", "derived"):
        with pytest.raises(InvalidRequest, match="must be measured"):
            MutationResolution("measure", method)


def test_a_run_s_resolution_record_keeps_its_bytes():
    record = MutationResolution("measure", "measured", "native measurement requested")
    assert json.dumps(record.to_dict()) == RUN_RESOLUTION
    assert MutationResolution.from_dict(json.loads(RUN_RESOLUTION)) == record
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.method = "measured"


@pytest.mark.parametrize("matrix", [None, {}, {"derived": {"kind": "test_subset",
                                                         "assumption": DERIVATION_ASSUMPTION}}])
def test_omitted_resolution_remains_unknown(matrix):
    assert resolution_of(matrix) is None


@pytest.mark.parametrize("key,value", [
    ("requested", "derive"), ("requested", None), ("method", "reused"),
    ("method", []), ("reason", None), ("conditions", {}), ("conditions", [None]),
    ("conditions", [{"name": "state", "passed": None, "reason": ""}]),
    ("prior_identity", {}),
])
def test_a_malformed_resolution_is_refused(key, value):
    data = MutationResolution("measure", "measured").to_dict()
    data[key] = value
    with pytest.raises(InvalidRequest):
        resolution_of({"resolution": data})


@pytest.mark.parametrize("value", [None, {}, [], "measured"])
def test_present_invalid_resolution_is_not_treated_as_omission(value):
    with pytest.raises(InvalidRequest):
        resolution_of({"resolution": value})


def test_an_unknown_field_is_refused():
    data = MutationResolution("measure", "measured").to_dict()
    data["guaranteed"] = True
    with pytest.raises(InvalidRequest):
        MutationResolution.from_dict(data)


def test_a_typed_subset_clears_the_parents_resolution():
    parent = dataclasses.replace(_run(), resolution=MutationResolution("measure", "measured"))
    matrix_before = parent.to_kill_matrix()
    derived = parent.derive_for_tests([TestId("test_target.py", "test_value")],
                                     covered_lines=[1])
    assert derived.resolution is None
    assert derived.derivation.assumption == DERIVATION_ASSUMPTION
    assert "resolution" not in derived.to_kill_matrix()
    assert parent.to_kill_matrix() == matrix_before
    assert parent.score() == derived.score() == 100
    with pytest.raises(InvalidRequest, match="assumed subset"):
        dataclasses.replace(derived, resolution=parent.resolution)


def test_saved_assumed_subset_cannot_claim_resolution():
    matrix = _run().to_kill_matrix()
    matrix["derived"] = {"kind": "test_subset", "assumption": DERIVATION_ASSUMPTION}
    matrix["resolution"] = MutationResolution("measure", "measured").to_dict()
    with pytest.raises(InvalidRequest, match="assumed subset"):
        resolution_of(matrix)
    with pytest.raises(InvalidRequest, match="assumed subset"):
        km.matrix_score_percent(matrix)


def test_matrix_readers_do_not_ignore_conflicting_metadata_even_if_incomplete():
    matrix = {"mutants": [], "resolution": None, "incomplete": {"pending": 1}}
    for reader in (km.matrix_score_percent, km.killed_ids, km.valid_ids, km.timeout_kill_count,
                   km.survivors_from_matrix, km.refresh_summary):
        with pytest.raises(InvalidRequest):
            reader(matrix)


def test_a_restricted_matrix_clears_resolution_without_changing_verdict_rules():
    parent = dataclasses.replace(_run(), resolution=MutationResolution("measure", "measured"))
    matrix = parent.to_kill_matrix()
    restricted = km.restrict_kill_matrix(matrix, kept_killed_ids=set())
    assert resolution_of(restricted) is None
    assert km.matrix_score_percent(restricted) == 0
    assert km.matrix_score_percent(matrix) == 100
    assert resolution_of(matrix) == parent.resolution


def test_serializer_refuses_injection_or_replacement_of_resolution():
    plain = _run()
    record = MutationResolution("measure", "measured")
    with pytest.raises(InvalidRequest, match="cannot replace"):
        plain.to_kill_matrix({"resolution": record.to_dict()})
    measured = dataclasses.replace(plain, resolution=record)
    with pytest.raises(InvalidRequest, match="cannot replace"):
        measured.to_kill_matrix({"resolution": None})
    assert resolution_of(measured.to_kill_matrix({"resolution": record.to_dict()})) == record
    with pytest.raises(InvalidRequest, match="assumed subset"):
        measured.to_kill_matrix({"derived": {"assumption": "test independence"}})


def test_failed_and_incomplete_measurement_keep_resolution_but_no_final_score():
    record = MutationResolution("measure", "measured", "native measurement requested")
    failed = dataclasses.replace(MutationRun.failed(
        "src/target.py", Outcome.MISSING_SETUP, "tool unavailable"), resolution=record)
    assert failed.score() is None
    assert resolution_of(failed.to_kill_matrix()) == record
    incomplete = dataclasses.replace(_run(), outcome=Outcome.TIMED_OUT,
                                      incomplete={"pending": 1}, resolution=record)
    assert incomplete.score() is None
    assert incomplete.progress_score() == 100
    assert km.matrix_score_percent(incomplete.to_kill_matrix()) is None
    assert resolution_of(incomplete.to_kill_matrix()) == record
