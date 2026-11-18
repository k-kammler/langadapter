"""Suites, test identifiers and evidence items."""
import hashlib

import pytest

import langadapter as lb


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_suite_with_a_setup_file_names_both_in_its_identity(tmp_path):
    test = _write(tmp_path / "tests" / "test_a.py", "def test_a(): pass\n")
    setup = _write(tmp_path / "tests" / "conftest.py", "")
    suite = lb.Suite(root=str(tmp_path), test_files=[str(test)],
                     setup_files=["tests/conftest.py"])
    suite.validate()
    assert suite.files() == (test, tmp_path / "tests" / "conftest.py")
    identity = suite.identity()
    assert [f.path for f in identity] == ["tests/test_a.py",
                                          "tests/conftest.py"]
    assert identity[0].sha256 == hashlib.sha256(test.read_bytes()).hexdigest()
    assert identity[1].sha256 == hashlib.sha256(setup.read_bytes()).hexdigest()


def test_a_directory_given_as_a_suite_is_refused(tmp_path):
    (tmp_path / "tests").mkdir()
    with pytest.raises(lb.InvalidRequest) as refused:
        lb.Suite(root=str(tmp_path), test_files=["tests"]).validate()
    assert refused.value.field == "suite.test_files"
    assert "directory" in refused.value.message


@pytest.mark.parametrize("files, field", [
    ([], "suite.test_files"),
    (["missing.py"], "suite.test_files"),
])
def test_an_incomplete_suite_is_refused(tmp_path, files, field):
    with pytest.raises(lb.InvalidRequest) as refused:
        lb.Suite(root=str(tmp_path), test_files=files).validate()
    assert refused.value.field == field


def test_a_file_outside_the_root_is_refused(tmp_path):
    outside = _write(tmp_path / "outside" / "test_x.py", "")
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(lb.InvalidRequest) as refused:
        lb.Suite(root=str(root), test_files=[str(outside)]).validate()
    assert "does not lie under" in refused.value.message


def test_test_identifiers_compare_by_their_parts():
    a = lb.TestId("tests/test_a.py", "TestA.test_x", "1-2",
                  text="tests/test_a.py::TestA::test_x[1-2]")
    b = lb.TestId("tests/test_a.py", "TestA.test_x", "1-2")
    assert a == b and hash(a) == hash(b)
    assert str(a) == "tests/test_a.py::TestA::test_x[1-2]"
    assert str(b) == "tests/test_a.py::TestA.test_x[1-2]"
    assert a != lb.TestId("tests/test_a.py", "TestA.test_x")
    assert len({a, b}) == 1


@pytest.mark.parametrize("file", ["", "C:\\tests\\test_a.py", "C:/tests/test_a.py", "/abs/test.py"])
def test_a_test_or_file_identity_needs_a_relative_posix_path(file):
    """A test identifier and a file identity take one path rule."""
    with pytest.raises(lb.InvalidRequest, match="must be a relative POSIX path"):
        lb.TestId(file, "test_a")
    with pytest.raises(lb.InvalidRequest, match="must be a relative POSIX path"):
        lb.FileIdentity(file, "0" * 64)


def test_the_canonical_tokens_equal_the_reduction_tokens():
    """The tokens are ``L:<file>:<line>``, ``B:<file>:<a>-><b>`` (a negative
    destination leaves a code object), ``K:<mutant>`` and, for a crash,
    ``C:<mutant>``."""
    line = lb.LineItem("groupby.py", 17)
    branch = lb.BranchItem("groupby.py", 17, 19)
    leave = lb.BranchItem("groupby.py", 23, -12)
    kill = lb.KillItem("5d1c9a8e0f2b4c11")
    crash = lb.CrashItem("5d1c9a8e0f2b4c11")
    assert line.token() == "L:groupby.py:17"
    assert branch.token() == "B:groupby.py:17->19"
    assert leave.token() == "B:groupby.py:23->-12"
    assert leave.leaves_code_object and not branch.leaves_code_object
    assert kill.token() == "K:5d1c9a8e0f2b4c11"
    assert crash.token() == "C:5d1c9a8e0f2b4c11" and crash != kill
    for item in (line, branch, leave, kill, crash):
        assert lb.parse_token(item.token()) == item


@pytest.mark.parametrize("token", [
    "L:file.py", "L:file.py:x", "B:file.py:1-2", "K:", "C:", "X:1", "",
])
def test_a_malformed_token_is_refused(token):
    with pytest.raises(lb.InvalidRequest):
        lb.parse_token(token)


def test_file_identities_are_relative(tmp_path):
    target = _write(tmp_path / "pkg" / "mod.py", "x = 1\n")
    identity = lb.FileIdentity.of(target, tmp_path)
    assert identity.path == "pkg/mod.py"
    with pytest.raises(lb.InvalidRequest):
        lb.FileIdentity("/abs/mod.py", "0" * 64)
    with pytest.raises(lb.InvalidRequest):
        lb.FileIdentity.of(target, tmp_path / "other")


def test_a_relative_root_is_resolved_when_the_suite_is_made(tmp_path, monkeypatch):
    # The runs start in other directories and would read a relative root there.
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "test_a.py").write_text("def test_a():\n    pass\n",
                                                  encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    suite = lb.Suite("suite", ("test_a.py",))
    assert suite.root == str(tmp_path / "suite")
    suite.validate()
