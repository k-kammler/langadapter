"""The Java adapter's public helpers for consumers: test ids and the
attribution of PIT killers, the normalization record and class renaming
without a JDK, and the benchmark of a suite."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.java import source_analysis
from langadapter.java import _mutation as attribution
from langadapter.runs import BenchmarkRequest
from langadapter.suites import TestId

def _test(file, qualname, text):
    return {"file": file, "qualname": qualname, "params": None, "text": text}


MATRIX = {"mutants": [
    {"id": "m1", "killers": ["demo.CalcTest#positive()", "demo.CalcTest$Clamping#keepsInside()"],
     "killing_tests": [
         _test("demo/CalcTest.java", "CalcTest.positive", "demo.CalcTest#positive()"),
         _test("demo/CalcTest.java", "CalcTest.Clamping.keepsInside",
               "demo.CalcTest$Clamping#keepsInside()")]},
    {"id": "m2", "killers": ["demo.CalcMoreTest#positives(int)[2]"], "killing_tests": [
        _test("demo/CalcMoreTest.java", "CalcMoreTest.positives",
              "demo.CalcMoreTest#positives(int)[2]")]},
    {"id": "m3", "killers": ["demo.Elsewhere#gone()"], "killing_tests": [],
     "unmatched_killers": ["demo.Elsewhere#gone()"]},
    {"id": "m4", "killers": []},
]}


def test_dotted_ids_and_the_attribution_of_killers(tmp_path):
    assert attribution.dotted_id("demo.CalcTest$Clamping#keepsInside()") == \
        "demo.CalcTest.Clamping.keepsInside"
    assert attribution.dotted_id("demo.CalcMoreTest#positives(int)[2]") == \
        "demo.CalcMoreTest.positives"
    assert attribution.dotted_id("no method") is None
    test = TestId("demo/CalcTest.java", "CalcTest.positive", None,
                  "demo.CalcTest#positive()")
    assert attribution.test_dotted_id(test) == "demo.CalcTest.positive"
    assert attribution.test_dotted_id(TestId("demo/CalcTest.java",
                                             "CalcTest.positive")) == \
        "demo.CalcTest.positive"
    java = lb.create("java", lb.ProjectContext(str(tmp_path)))
    ids = {"demo.CalcTest.positive", "demo.CalcTest.Clamping.keepsInside",
           "demo.CalcMoreTest.positives"}
    items, unmatched = java.kill_tokens(MATRIX, ids)
    assert items == {"demo.CalcTest.positive": {"K:m1"},
                     "demo.CalcTest.Clamping.keepsInside": {"K:m1"},
                     "demo.CalcMoreTest.positives": {"K:m2"}}
    assert unmatched == ("demo.Elsewhere#gone()",)
    files = {"CalcTest.java": 0, "CalcMoreTest.java": 1}
    assert java.pool_kills(
        MATRIX, files, {0: {"demo.CalcTest.positive"},
                        1: {"demo.CalcMoreTest.positives"}}) == {
        0: {"demo.CalcTest.positive": {"K:m1"}},
        1: {"demo.CalcMoreTest.positives": {"K:m2"}}}


DUPLICATED = """\
package demo;

class CalcTest {
    int base;

    void zero() {
    }

    int base = 2;

    void zero() {
        base = 0;
    }
}
"""


def test_the_normalization_records_what_it_removed_and_kept():
    result = source_analysis.normalize_shadowed_definitions(DUPLICATED)
    assert result.parse_ok and result.changed
    assert result.rule_version == "java-last-declaration-v1"
    removed = {(b.scope, b.name, b.kind, b.removed_start_line, b.retained_start_line)
               for b in result.removed_bindings}
    assert removed == {(("CalcTest",), "base", "field", 4, 9),
                       (("CalcTest",), "zero", "method", 6, 11)}
    again = source_analysis.normalize_shadowed_definitions(result.source)
    assert not again.changed and again.source == result.source
    broken = source_analysis.normalize_shadowed_definitions("class X {")
    assert not broken.parse_ok and broken.source == "class X {"


def test_a_class_is_renamed_with_its_uses():
    source = ("package demo;\n\nimport demo.CalcTest.Helper;\n\n"
              "public class CalcTest {\n    CalcTest() {\n    }\n\n"
              "    static CalcTest make() {\n        return new CalcTest();\n"
              "    }\n\n    void CalcTest2() {\n        CalcTest.make();\n    }\n}\n")
    observed = source_analysis.rename_class(source, "CalcTest", "CalcSeed1Test")
    assert observed.completed
    renamed = observed.value
    assert "import demo.CalcTest.Helper;" in renamed
    assert "public class CalcSeed1Test {" in renamed
    assert "CalcSeed1Test() {" in renamed and "new CalcSeed1Test()" in renamed
    assert "CalcSeed1Test.make();" in renamed
    assert source_analysis.top_class_name(renamed) == "CalcSeed1Test"
    assert not source_analysis.rename_class("class X {", "X", "Y").completed


def test_a_suite_is_benchmarked_over_repeated_launches(java_project, jdks, maven):
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    project = java_project("maven")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))
    events = []
    result = adapter.benchmark(BenchmarkRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       ("demo/CalcTest.java",)),
        runs=3, warmup_runs=1, timeout_s=300, progress=events.append))
    assert result.outcome is lb.Outcome.COMPLETED, result.reason
    assert len(result.times_ms) == 3 and all(t > 0 for t in result.times_ms)
    assert result.median_ms == sorted(result.times_ms)[1]
    assert [e.kind for e in events][0] == "warmup"
    assert "JVM" in result.includes
