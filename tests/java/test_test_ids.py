"""A Java test's id is built once (_tools.test_id): a suite run's report case
and a kill's name give the same TestId, from any spelling of the suite's
root."""
from __future__ import annotations

from pathlib import Path

from langadapter.java import _tools
from langadapter.java.adapter import _test_id


def test_a_report_case_and_a_named_test_give_one_id_from_any_root_spelling(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    file = tmp_path / "suite" / "demo" / "CalcTest.java"
    file.parent.mkdir(parents=True)
    file.write_text("", encoding="utf-8")
    classes = {"demo.CalcTest": file}
    expected = ("demo/CalcTest.java", "CalcTest.Inner.adds", "[2]",
                "demo.CalcTest$Inner#adds(int)[2]")

    for root in (tmp_path / "suite", Path("suite")):
        named = _tools.test_id(_tools.TestName("demo.CalcTest$Inner", "adds(int)", "[2]"),
                               classes, root)
        reported = _test_id(_tools.Case("demo.CalcTest$Inner", "adds(int)[2]", "passed",
                                        0.01, ""), classes, str(root))
        assert named == reported
        assert (named.file, named.qualname, named.params, named.text) == expected
    assert _test_id(_tools.Case("demo.OtherTest", "x()", "passed", 0.0, ""),
                    classes, str(tmp_path)) is None
