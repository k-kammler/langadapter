"""Test identifiers of the Python adapter from the node ids pytest prints."""
from types import SimpleNamespace

from langadapter.python._cosmic_ray import MutationSession
from langadapter.python._runs import identify_test

FILES = ["test_a.py", "unit/test_b.py"]


def test_a_node_id_relative_to_any_directory_names_the_suites_test():
    test = identify_test("../../scratch/proj/tests/test_a.py::test_x", FILES)
    assert (test.file, test.qualname, test.params) == ("test_a.py", "test_x", None)
    assert str(test) == "test_a.py::test_x"


def test_classes_parameters_and_windows_paths():
    test = identify_test("..\\tmp\\unit\\test_b.py::TestC::test_m[1-a]", FILES)
    assert (test.file, test.qualname, test.params) == (
        "unit/test_b.py", "TestC.test_m", "1-a")
    assert str(test) == "unit/test_b.py::TestC::test_m[1-a]"


def test_a_file_outside_the_suite_names_no_test():
    assert identify_test("elsewhere/test_c.py::test_x", FILES) is None
    assert identify_test("test_a.py", FILES) is None


def test_a_runs_killers_are_tests_unmatched_killers_or_file_attributions():
    mutant = SimpleNamespace(killers=(
        "../../tmp/x/test_a.py::test_x",
        "elsewhere/test_c.py::test_y",
        "unit/test_b.py::per_file_adjudication"))
    tests, unmatched = MutationSession._placed_killers(mutant, FILES)
    assert [str(t) for t in tests] == ["test_a.py::test_x"]
    assert unmatched == ("elsewhere/test_c.py::test_y",)


def test_a_run_keeps_its_killers_as_the_suites_test_ids():
    """A killer that pytest names from where the run's copy of the suite
    lies (../../dev/shm/.../suite_x/...) is kept as the suite's own test
    id, and an unmatched one as reported."""
    killers = ("../../dev/shm/cr_1/suite_x/unit/test_b.py::TestC::test_m[1]",
               "elsewhere/test_c.py::test_y", "unit/test_b.py::per_file_adjudication")
    assert [MutationSession._suite_killer(k, FILES) for k in killers] == [
        "unit/test_b.py::TestC::test_m[1]", "elsewhere/test_c.py::test_y",
        "unit/test_b.py::per_file_adjudication"]


def test_a_parameter_id_that_holds_double_colons_is_read_as_pytest_writes_it():
    """pytest keeps '::' inside a parameter id (test_p[a::b]), so the
    parameters start at the first '[' after the file, in every reading."""
    from langadapter.python._plugins import test_of_nodeid
    from langadapter.python._runs import node_function

    nodeid = "unit/test_b.py::TestC::test_p[a::b]"
    test = test_of_nodeid(nodeid)
    assert (test.file, test.qualname, test.params) == ("unit/test_b.py", "TestC.test_p", "a::b")
    assert node_function(nodeid) == "unit.test_b.TestC.test_p"
    found = identify_test(nodeid, FILES)
    assert (found.file, found.qualname, found.params) == ("unit/test_b.py", "TestC.test_p", "a::b")
