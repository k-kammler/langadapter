"""The Cosmic Ray mapping from a mutant's native record to its kill-matrix
status, common class and subcategory."""

import pytest

from langadapter.mutation import (
    DETECTED_STATUSES, Classification, MutationClass,
)
from langadapter.python._cr_verify import classify_outcome

DETECTED, UNDETECTED = MutationClass.DETECTED, MutationClass.UNDETECTED
NOT_RUN = MutationClass.NOT_RUN

CASES = [
    (("KILLED", "SKIPPED", "Filtered operator"), (), ("skipped", NOT_RUN, "skipped")),
    ((None, None, None), (), ("untested", NOT_RUN, "untested")),
    (("KILLED", "NORMAL", "timeout"), (), ("timeout", DETECTED, "timeout")),
    (("KILLED", "NORMAL", " Timeout\n"), (), ("timeout", DETECTED, "timeout")),
    (("KILLED", "NORMAL", "FAILED t.py::a"), ("t.py::a",),
     ("killed", DETECTED, "failing_test")),
    (("KILLED", "NORMAL", "Segmentation fault"), (),
     ("killed", DETECTED, "no_named_test")),
    (("SURVIVED", "NORMAL", "1 passed"), (), ("survived", UNDETECTED, "survived")),
    # Running the tests (worker NORMAL) or applying the mutation (worker
    # EXCEPTION) raised, so nothing was measured.
    (("INCOMPETENT", "NORMAL", "UnicodeDecodeError"), (),
     ("untested", NOT_RUN, "incompetent")),
    (("INCOMPETENT", "EXCEPTION", "Traceback"), (),
     ("untested", NOT_RUN, "incompetent")),
    # No test ran, so nothing detected the mutant: ABNORMAL is a failed HTTP
    # worker request, NO_TEST a mutation that could not be applied.
    ((None, "EXCEPTION", "Traceback"), (), ("untested", NOT_RUN, "untested")),
    ((None, "ABNORMAL", "Cannot connect to host"), (), ("untested", NOT_RUN, "untested")),
    ((None, "NO-TEST", None), (), ("untested", NOT_RUN, "untested")),
    (("UNKNOWN", "NORMAL", ""), (), ("untested", NOT_RUN, "untested")),
]


@pytest.mark.parametrize("native, killers, expected", CASES)
def test_each_native_record_maps_as_documented(native, killers, expected):
    classification = classify_outcome(*native, killers)
    assert (classification.status, classification.mutation_class,
            classification.subcategory) == expected


def test_the_detected_class_holds_the_statuses_a_score_counts_as_killed():
    """cr-rate's killed set, ``DETECTED_STATUSES``, is the class DETECTED."""
    assert DETECTED_STATUSES == {"killed", "timeout", "non_viable"}
    for status, mutation_class, _ in (expected for _, _, expected in CASES):
        assert (mutation_class is DETECTED) == (status in DETECTED_STATUSES)


def test_a_subcategory_belongs_to_its_class():
    with pytest.raises(ValueError, match="not one of class"):
        Classification("killed", DETECTED, "survived")
