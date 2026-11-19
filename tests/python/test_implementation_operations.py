"""Python implementation operations: per-test evidence from coverage
contexts and JUnit ids, killer parsing and pytest's counts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.python._plugins import render
from langadapter.python._runs import (
    MeasurementDataUnreadable, record_collected_nothing, record_counts, record_killers,
    record_reported, run_record_in, run_record_mark,
)
from langadapter.python import _runs
from langadapter.python.adapter import PythonAdapter
from langadapter.python._output import _pytest_failure_kind


def _adapter():
    """The implementation for a local project in the calling interpreter."""
    return PythonAdapter(lb.ProjectContext(
        root=str(Path(__file__).parent),
        interpreter=lb.Interpreter(path=sys.executable)))


def test_eviction_times_reconcile_and_sum_at_full_resolution(tmp_path):
    """A test's time in milliseconds is the sum of pytest's phase durations
    at full clock resolution, keyed by the JUnit name (dotted module path,
    classes, function). Parametrized cases are summed under one id. An
    unreadable record, or one without coverage measurement, is refused."""
    import json

    record = tmp_path / "times.json"
    durations = {
        "test_mod.py::test_plain": 0.0004321,
        "tests/sub/test_mod.py::TestG::test_method": 0.002,
        "test_mod.py::test_param[1]": 0.0002,
        "test_mod.py::test_param[2]": 0.0003,
    }
    record.write_text(json.dumps({"durations": durations, "contexts": True}),
                      encoding="utf-8")
    times = _runs._eviction_times(str(record))
    assert times["test_mod.test_plain"] == pytest.approx(0.4321)
    assert times["tests.sub.test_mod.TestG.test_method"] == pytest.approx(2.0)
    assert times["test_mod.test_param"] == pytest.approx(0.5)
    with pytest.raises(MeasurementDataUnreadable, match="per-test times"):
        _runs._eviction_times(str(tmp_path / "nope.json"))
    unmeasured = tmp_path / "unmeasured.json"
    unmeasured.write_text(json.dumps({"durations": durations, "contexts": False}),
                          encoding="utf-8")
    with pytest.raises(MeasurementDataUnreadable, match="found no coverage measurement"):
        _runs._eviction_times(str(unmeasured))


def test_match_context_to_test_id_handles_package_prefix():
    """A coverage context maps to a JUnit id by exact match or a suffix at a
    ``.`` boundary, never inside a component."""
    # tests/ layout: coverage drops the path prefix, JUnit keeps it
    ids = {"tests.test_arith.test_add", "tests.test_arith.test_neg"}
    assert (
        _runs._match_context_to_test_id("test_arith.test_add", ids)
        == "tests.test_arith.test_add"
    )
    # flat layout: exact match
    assert (
        _runs._match_context_to_test_id(
            "test_mod.test_x", {"test_mod.test_x"}
        )
        == "test_mod.test_x"
    )
    # "arith" must not match inside "myarith"
    assert (
        _runs._match_context_to_test_id(
            "arith.test_add", {"tests.myarith.test_add"}
        )
        is None
    )
    assert _runs._match_context_to_test_id("x.y", set()) is None


def test_eviction_tokens_from_coverage_excludes_import_context(tmp_path):
    """Per-test ``L:`` and ``B:`` tokens come from a branch coverage file's
    contexts, without the empty import-time context. A missing file is
    refused."""
    from coverage import CoverageData

    target = str(tmp_path / "mod.py")
    data_file = str(tmp_path / "cov")
    d = CoverageData(basename=data_file)
    d.set_context("test_mod.test_pos")
    d.add_arcs({target: [(-1, 2), (2, 3), (3, 4)]})
    d.set_context("")  # import time, no test
    d.add_arcs({target: [(-1, 2)]})
    d.write()

    toks = _runs._eviction_tokens_from_coverage(_adapter(), data_file, target)
    assert set(toks) == {"test_mod.test_pos"}
    t = toks["test_mod.test_pos"]
    # lines are the positive arc endpoints, branches the arcs
    assert "L:mod.py:4" in t
    assert "L:mod.py:2" in t
    assert "B:mod.py:3->4" in t
    assert "B:mod.py:-1->2" in t
    with pytest.raises(MeasurementDataUnreadable, match="no coverage data file"):
        _runs._eviction_tokens_from_coverage(_adapter(), str(tmp_path / "absent"), target)
    with pytest.raises(MeasurementDataUnreadable, match="no coverage data file"):
        _runs._covered_statements(_adapter(), str(tmp_path / "absent"), target, str(tmp_path))


def test_the_coverage_readers_start_the_interpreter_with_its_flags(tmp_path):
    """The coverage readers start the project interpreter with the context's
    flags. With -S it has no site packages, so no coverage.py, and each
    reader fails on data it reads without the flag."""
    from coverage import CoverageData

    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    data_file = str(tmp_path / "cov")
    d = CoverageData(basename=data_file)
    d.set_context("test_mod.test_pos")
    d.add_arcs({str(target): [(-1, 1)]})
    d.write()
    flagged = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable),
        interpreter_flags=("-S",)))
    with pytest.raises(MeasurementDataUnreadable, match="No module named 'coverage'"):
        _runs._eviction_tokens_from_coverage(flagged, data_file, str(target))
    with pytest.raises(MeasurementDataUnreadable, match="No module named 'coverage'"):
        _runs._covered_statements(flagged, data_file, str(target), str(tmp_path))
    report = tmp_path / "report.json"
    with pytest.raises(FileNotFoundError):
        _runs._coverage_json(flagged, data_file, str(report), str(target),
                             dict(os.environ), str(tmp_path))


def test_the_killers_are_the_records_failed_then_erroneous_tests():
    """The run record names the killers as pytest's short summary does: the
    failed tests, then the erroneous ones, each once, a parametrized id
    whole even with spaces in its parameters. A collection node (no ``::``)
    names no test."""
    record = {"failed": ["test_x.py::test_case[a b]", "test_x.py::test_plain",
                         "test_x.py::test_case[a b]"],
              "errors": ["test_x.py::TestC::test_m[x - y]", "test_x.py"]}
    assert record_killers(record) == (
        "test_x.py::test_case[a b]", "test_x.py::test_plain",
        "test_x.py::TestC::test_m[x - y]")
    assert record_killers({"failed": [], "errors": []}) == ()
    assert record_killers(None) == ()
    # The summary's report characters order the groups (-ra: "Ef"). Where
    # they list neither (-rN), failures come first.
    ordered = {"failed": ["a.py::t_fail"], "errors": ["a.py::t_error"]}
    assert record_killers({**ordered, "order": "Ef"}) == ("a.py::t_error", "a.py::t_fail")
    assert record_killers({**ordered, "order": ""}) == ("a.py::t_fail", "a.py::t_error")


def test_the_run_record_is_its_last_marked_line_and_text_names_none():
    """Only the record's own line counts: a test's printed summary or FAILED
    line names no killer, the last record line is the run's, and a broken
    line is no record."""
    mark = run_record_mark()
    printed = "FAILED test_x.py::test_fail_one - assert 1 == 2\n1 failed, 1 passed in 0.13s\n"
    assert run_record_in(printed) is None
    assert record_killers(run_record_in(printed)) == ()
    output = (printed + mark + '{"failed": ["a::t1"], "errors": []}\n'
              + mark + '{"failed": ["b::t2"], "errors": [], "counts": {}}\n')
    assert run_record_in(output) == {
        "failed": ["b::t2"], "errors": [], "counts": {}}
    assert run_record_in(printed + mark + "{not json\n") is None


def test_the_record_counts_are_the_quiet_summary_counts():
    """passed, failed and errors as pytest's summary counts them, and as
    collected their sum less one per file pytest could not collect. All are
    zero without a record, and "reported" appears only with one."""
    record = {"counts": {"passed": 3, "failed": 1, "error": 2, "skipped": 4, "xpassed": 1},
              "broken": ["test_b.py", "test_b.py"]}
    assert record_counts(record) == {"collected": 5, "passed": 3, "failed": 1, "errors": 2}
    assert record_counts(None) == {"collected": 0, "passed": 0, "failed": 0, "errors": 0}
    assert record_reported(record) and not record_reported(None)
    assert record_collected_nothing({"counts": {}})
    assert not record_collected_nothing({"counts": {"warnings": 1}})
    assert not record_collected_nothing(None)


def test_a_red_suite_is_read_from_pytest_summary_not_from_test_output():
    """Exit 1 is a red suite only when pytest's closing summary counts a
    failure or an error, not when a test prints similar text."""
    printed = ("test_a.py some text: 1 failed, 2 errors in the fixture\n"
               "=== 2 passed in 0.10s ===\n")
    assert _pytest_failure_kind(printed, 1) == "infrastructure"
    red = "FAILED test_a.py::test_b\n=== 1 failed, 1 passed in 0.10s ===\n"
    assert _pytest_failure_kind(red, 1) == "test_failure"
    assert _pytest_failure_kind(red, 2) == "infrastructure"




def test_the_run_record_plugin_writes_the_mark_the_reader_looks_for():
    """The record plugin's line and run_record_mark come from one template."""
    from langadapter.python._plugins import RUN_RECORD_MARK, run_record_text

    assert run_record_mark() == render(RUN_RECORD_MARK) == "LANGADAPTER_RUN_RECORD "
    assert run_record_mark() in run_record_text()
