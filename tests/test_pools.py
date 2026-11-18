"""Pooled staging of several suites: file names per language, Java's
top-level types, the way back to each suite's standalone identities, and the
pools that cannot be staged: adapter-owned staging, valid file names, Java
class names and collisions."""
from __future__ import annotations

import sys

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.sources import PoolMember, StagedPool
from langadapter.sources import SourceStatus
from langadapter.suites import TestId

PYTHON_A = "def test_a():\n    assert 1\r\n"
PYTHON_B = "def test_a():\n    assert 2\n"

CALC_TEST = """\
package demo;

import org.junit.jupiter.api.Test;

public class CalcTest {
    static CalcTest make() { return new CalcTest(); }
    CalcTest() {}
    @Test void adds() { Helper.go(); }
    static class Inner { @Test void deep() {} }
}

class Helper { static void go() {} }
"""


def _python(tmp_path):
    return lb.create("python", lb.ProjectContext(
        str(tmp_path), lb.Interpreter(path=sys.executable)))


@pytest.fixture
def java_sources(tmp_path):
    """A Java adapter's source group without a JDK, since staging reads
    only the texts."""
    return lb.create("java", lb.ProjectContext(str(tmp_path))).sources


def test_python_files_take_their_slot_and_keep_their_text(tmp_path):
    observation = _python(tmp_path).sources.stage_pool(
        (PoolMember("seed_000", "test_candidate.py", PYTHON_A),
         PoolMember("seed_001", "test_candidate.py", PYTHON_B),
         PoolMember("seed_002", "candidate_check.py", PYTHON_B)),
        setup=(("conftest.py", "# shared\n"),))
    assert observation.status is SourceStatus.COMPLETED
    pool = observation.value
    assert isinstance(pool, StagedPool) and pool.slots == ("seed_000", "seed_001", "seed_002")
    assert [f.path for f in pool.files] == [
        "test_candidate_seed_000.py", "test_candidate_seed_001.py",
        "test_candidate_check_seed_002.py"]
    assert [f.source for f in pool.files] == [PYTHON_A, PYTHON_B, PYTHON_B]
    assert pool.files[0].sha256 != pool.files[1].sha256
    root = tmp_path / "pool"
    root.mkdir()
    suite = pool.write(str(root))
    suite.validate()
    assert suite.test_files == tuple(f.path for f in pool.files)
    assert suite.setup_files == ("conftest.py",)
    # The text is written as given, its line endings included.
    assert (root / "test_candidate_seed_000.py").read_bytes() == PYTHON_A.encode()


def test_a_pooled_python_test_and_killer_read_as_the_standalone_run_names_them(tmp_path):
    pool = _python(tmp_path).sources.stage_pool(
        (PoolMember("seed_000", "test_candidate.py", PYTHON_A),
         PoolMember("seed_001", "test_candidate.py", PYTHON_B))).value
    staged = TestId("test_candidate_seed_001.py", "TestX.test_a", "1",
                    "test_candidate_seed_001.py::TestX::test_a[1]")
    assert pool.original_test(staged) == ("seed_001", TestId(
        "test_candidate.py", "TestX.test_a", "1", "test_candidate.py::TestX::test_a[1]"))
    killer = "../work/test_candidate_seed_000.py::test_a"
    assert pool.slot_of_killer(killer) == "seed_000"
    assert pool.original_text(killer, "seed_000") == "../work/test_candidate.py::test_a"
    assert pool.slot_of_killer("../work/test_other.py::test_a") is None
    with pytest.raises(InvalidRequest, match="none of the pooled files"):
        pool.original_test(TestId("test_other.py", "test_a"))


def test_java_types_take_their_slot_and_files_their_class_name(java_sources):
    """Two starter suites that declare the same class and helper in one
    package get renamed types so they do not collide."""
    observation = java_sources.stage_pool(
        (PoolMember("seed_000", "CalcTest.java", CALC_TEST),
         PoolMember("seed_001", "CalcTest.java", CALC_TEST)))
    assert observation.status is SourceStatus.COMPLETED
    pool = observation.value
    assert [f.path for f in pool.files] == ["CalcTest_seed_000.java", "CalcTest_seed_001.java"]
    assert pool.files[1].renamed == (("CalcTest", "CalcTest_seed_001"),
                                     ("Helper", "Helper_seed_001"))
    text = pool.files[1].source
    assert "public class CalcTest_seed_001 {" in text
    assert "static CalcTest_seed_001 make() { return new CalcTest_seed_001(); }" in text
    assert "CalcTest_seed_001() {}" in text and "Helper_seed_001.go();" in text
    assert "class Helper_seed_001 {" in text and "static class Inner {" in text
    assert "package demo;" in text and "CalcTest " not in text.replace("CalcTest_seed_001", "")


def test_a_pooled_java_test_and_killer_read_as_the_standalone_run_names_them(java_sources):
    pool = java_sources.stage_pool(
        (PoolMember("seed_000", "CalcTest.java", CALC_TEST),
         PoolMember("seed_001", "CalcTest.java", CALC_TEST))).value
    staged = TestId("CalcTest_seed_001.java", "CalcTest_seed_001.Inner.deep", None,
                    "demo.CalcTest_seed_001$Inner#deep()")
    assert pool.original_test(staged) == ("seed_001", TestId(
        "CalcTest.java", "CalcTest.Inner.deep", None, "demo.CalcTest$Inner#deep()"))
    assert pool.slot_of_killer("demo.CalcTest_seed_000#adds()") == "seed_000"
    assert pool.original_text("demo.CalcTest_seed_000#adds()", "seed_000") == "demo.CalcTest#adds()"


@pytest.mark.parametrize("members, status, reason", [
    ((PoolMember("a", "CalcTest.java", "class CalcTest {"),),
     SourceStatus.PARSE_ERROR, "does not parse"),
    ((PoolMember("a", "Other.java", CALC_TEST),),
     SourceStatus.UNSUPPORTED, "not named for its public class"),
    ((PoolMember("a", "CalcTest.java", CALC_TEST + "class Helper_a {}\n"),),
     SourceStatus.UNSUPPORTED, "already declares Helper_a"),
    # Each suite's renaming is unique, but slot "b_a" renames X to X_b_a,
    # which slot "a" makes of its own X_b.
    ((PoolMember("b_a", "XTest.java", "package p;\npublic class XTest {}\nclass X {}\n"),
      PoolMember("a", "YTest.java", "package p;\npublic class YTest {}\nclass X_b {}\n")),
     SourceStatus.UNSUPPORTED, "would collide: p.X_b_a"),
    ((PoolMember("a", "notes.txt", ""),), SourceStatus.UNSUPPORTED, "not a Java file"),
])
def test_a_java_pool_that_cannot_be_staged_says_why(java_sources, members, status, reason):
    observation = java_sources.stage_pool(members)
    assert observation.status is status and reason in observation.reason


def test_a_python_pool_whose_names_would_collide_is_unsupported(tmp_path):
    sources = _python(tmp_path).sources
    observation = sources.stage_pool((PoolMember("1", "test_a_2.py", ""),
                                       PoolMember("2_1", "test_a.py", "")))
    assert observation.status is SourceStatus.UNSUPPORTED
    assert "test_a_2_1.py" in observation.reason
    observation = sources.stage_pool((PoolMember("a", "notes.txt", ""),))
    assert observation.status is SourceStatus.UNSUPPORTED


@pytest.mark.parametrize("members, setup, field", [
    ((), (), "members"),
    ((PoolMember("seed-1", "test_a.py", ""),), (), "members.slot"),
    ((PoolMember("a", "/abs/test_a.py", ""),), (), "members.name"),
    ((PoolMember("a", "../test_a.py", ""),), (), "members.name"),
    ((PoolMember("a", "test_a.py", ""), PoolMember("a", "test_a.py", "")), (), "members"),
    ((PoolMember("a", "test_a.py", ""),), (("conftest.py", ""), ("conftest.py", "")), "setup"),
    ((PoolMember("a", "test_a.py", ""),), ("conftest.py",), "setup"),
])
def test_a_request_no_pool_could_take_is_refused(tmp_path, members, setup, field):
    with pytest.raises(InvalidRequest) as refused:
        _python(tmp_path).sources.stage_pool(members, setup=setup)
    assert refused.value.field == field


def test_stage_pool_is_a_declared_source_operation(tmp_path, java_sources):
    assert _python(tmp_path).sources.supports("stage_pool")
    assert java_sources.supports("stage_pool")


def test_a_suites_matrix_derived_from_a_pooled_run_names_its_own_killers(tmp_path):
    """The reductions' rule (restrict_kill_matrix), applied with the mutants
    a suite's tests killed in the pool and the lines they cover."""
    pool = _python(tmp_path).sources.stage_pool(
        (PoolMember("seed_000", "test_candidate.py", PYTHON_A),
         PoolMember("seed_001", "test_candidate.py", PYTHON_B))).value
    first, second = "../w/test_candidate_seed_000.py::test_a", "../w/test_candidate_seed_001.py::test_a"
    tests = [{"file": "test_candidate_seed_000.py", "qualname": "test_a", "params": None, "text": first},
             {"file": "test_candidate_seed_001.py", "qualname": "test_a", "params": None, "text": second}]
    pooled = {"mutants": [
        {"id": 1, "line": 3, "status": "killed", "killer": first, "killers": [first, second],
         "killing_tests": tests},
        {"id": 2, "line": 4, "status": "killed", "killer": second, "killers": [second],
         "killing_tests": tests[1:]},
        {"id": 3, "line": 7, "status": "timeout", "killer": None, "killers": []},
        {"id": 4, "line": 8, "status": "non_viable", "killer": None, "killers": []},
        {"id": 5, "line": 9, "status": "survived", "killer": None, "killers": []},
    ]}
    assert pool.killed_by(pooled, "seed_000") == {1}
    derived = pool.derive(pooled, "seed_000", covered_lines={7})
    assert [(row["id"], row["status"], row["killers"]) for row in derived["mutants"]] == [
        (1, "killed", ["../w/test_candidate.py::test_a"]),
        (2, "survived", []),
        (3, "timeout", []),
        (4, "non_viable", []),
        (5, "survived", []),
    ]
    assert derived["derived"]["kind"] == "kill_subset"
    assert derived["derived"]["pool_slot"] == "seed_000"
    assert [row["derived"]["rule"] for row in derived["mutants"]][:3] == [
        "kept_killer", "no_kept_killer", "covered_line"]
    other = pool.derive(pooled, "seed_001", covered_lines=())
    assert [row["status"] for row in other["mutants"]] == [
        "killed", "killed", "survived", "non_viable", "survived"]
    assert other["mutants"][0]["killers"] == ["../w/test_candidate.py::test_a"]
    # The killing tests as the suite's standalone run names them.
    assert [row.get("killing_tests") for row in derived["mutants"][:2]] == [
        [{"file": "test_candidate.py", "qualname": "test_a", "params": None,
          "text": "../w/test_candidate.py::test_a"}], None]
    with pytest.raises(InvalidRequest, match="no suite is staged"):
        pool.derive(pooled, "seed_009", covered_lines=())


def test_a_suite_that_refers_to_its_own_name_says_so(tmp_path, java_sources):
    """The pool renames Java classes and Python modules, so a test that
    reads or prints its own name may see another: the staging says where
    and stages the suite all the same."""
    java = CALC_TEST.replace(
        "@Test void adds() { Helper.go(); }",
        '@Test void adds() { Helper.go(); assert "CalcTest".equals(getClass().getSimpleName()); }')
    java = java.replace("import org.junit.jupiter.api.Test;",
                        "import org.junit.jupiter.api.Test;\nimport static demo.Helper.go;")
    pool = java_sources.stage_pool((PoolMember("a", "CalcTest.java", java),
                                    PoolMember("b", "CalcTest.java", CALC_TEST))).value
    assert pool.files[0].self_references == (
        "imports its own class Helper", "names its class CalcTest in a string",
        "reads a class name at run time")
    assert pool.files[1].self_references == ()
    python = ("import os\n\n\n"
              "def test_here():\n    assert os.path.exists(__file__)\n"
              "    assert 'test_candidate' in repr(test_here.__module__)\n\n\n"
              "if __name__ == '__main__':\n    test_here()\n")
    pool = _python(tmp_path).sources.stage_pool(
        (PoolMember("a", "test_candidate.py", python),
         PoolMember("b", "test_candidate.py", PYTHON_B))).value
    assert pool.files[0].self_references == (
        "names its module test_candidate in a string", "reads its module's __file__",
        "reads its module's __module__")
    assert pool.files[1].self_references == ()


@pytest.mark.parametrize("sub_dir, statement, imported", [
    ("", "from helpers import make", "helpers"),
    ("", "import helpers", "helpers"),
    ("", "import helpers as aid", "helpers"),
    ("", "def test_late():\n    from helpers import make", "helpers"),
    ("sub/", "from . import helpers", "sub.helpers"),
    ("sub/", "from .helpers import make", "sub.helpers"),
    ("sub/", "import sub.helpers", "sub.helpers"),
    ("sub/", "from sub import helpers", "sub.helpers"),
    ("", "import test_calc", "test_calc"),
])
def test_a_python_suite_that_imports_its_own_file_is_not_pooled(tmp_path, sub_dir, statement,
                                                                 imported):
    """The staging renames every file of a suite, helpers included, so a
    pool whose suite imports its own file is refused, naming the import."""
    test = f"{statement}\n\n\ndef test_make():\n    assert True\n"
    observation = _python(tmp_path).sources.stage_pool(
        (PoolMember("a", f"{sub_dir}test_calc.py", test),
         PoolMember("a", f"{sub_dir}helpers.py", "def make():\n    return 1\n"),
         PoolMember("b", "test_calc.py", PYTHON_B)))
    assert observation.status is SourceStatus.UNSUPPORTED
    own = "test_calc.py" if imported == "test_calc" else "helpers.py"
    assert observation.reason == (
        f"{sub_dir}test_calc.py of suite a imports {imported}, its suite's {sub_dir}{own}, "
        f"which the pool renames to {sub_dir}test_{own[:-3].removeprefix('test_')}_a.py")


def test_a_python_suite_that_imports_its_own_package_is_not_pooled(tmp_path):
    observation = _python(tmp_path).sources.stage_pool(
        (PoolMember("a", "sub/test_calc.py", "import sub\n"),
         PoolMember("a", "sub/__init__.py", "VALUE = 1\n")))
    assert observation.status is SourceStatus.UNSUPPORTED
    assert observation.reason.startswith(
        "sub/test_calc.py of suite a imports sub, its suite's sub/__init__.py")


def test_a_python_suite_that_imports_other_modules_is_pooled(tmp_path):
    """Imports of the project, of shared setup files and of another suite's
    names stay as they are, and a suite's module named in a string is
    flagged rather than refused."""
    test = ("import importlib\nimport conftest\nfrom calc import add\nfrom . import util\n\n\n"
            "def test_add():\n    importlib.import_module('helpers')\n"
            "    assert add(1, 2) == 3\n")
    observation = _python(tmp_path).sources.stage_pool(
        (PoolMember("a", "test_calc.py", test),
         PoolMember("a", "helpers.py", "VALUE = 1\n"),
         PoolMember("b", "util.py", "VALUE = 2\n")),
        setup=(("conftest.py", "# shared\n"),))
    assert observation.status is SourceStatus.COMPLETED, observation.reason
    assert observation.value.files[0].self_references == (
        "names its suite's module helpers in a string",)
    assert observation.value.files[2].self_references == ()
