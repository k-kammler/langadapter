"""Files a Java test run creates in the prepared copy are removed before the
next run that asks for it (``remove_created_files``), as in Python. The
project's build output and files written between runs stay."""
from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

import langadapter as lb
from langadapter._prepared_copy import CreatedFiles
from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest

#: A test that fails when its file is already in the test JVM's working directory.
WRITES = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

class WritesTest {
    @Test
    void writesOnce() throws Exception {
        assertEquals(1, Calc.sign(5));
        Path made = Path.of("made.txt");
        assertFalse(Files.exists(made), "an earlier run's file is still there");
        Files.writeString(made, "x");
    }
}
"""


#: A test that writes the same file and then runs past any timeout.
HANGS = """\
package demo;

import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

class HangsTest {
    @Test
    void writesThenHangs() throws Exception {
        Files.writeString(Path.of("made.txt"), "x");
        Thread.sleep(600_000);
    }
}
"""


def _prepared(java_project, jdks, maven, gradle, tmp_path):
    java = jdks[max(jdks)] / "bin" / ("java.exe" if os.name == "nt" else "java")
    project = java_project("maven")
    (project / "src" / "test" / "java" / "demo" / "WritesTest.java").write_text(
        WRITES, encoding="utf-8")
    owner = lb.prepare_project(lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    return owner, project


def _suite(copy: Path) -> lb.Suite:
    return lb.Suite(str(copy / "src" / "test" / "java"), ("demo/WritesTest.java",))


@pytest.mark.parametrize("remove", [True, False])
def test_files_a_java_run_created_are_removed_only_when_asked(
        java_project, jdks, maven, gradle, tmp_path, remove):
    owner, project = _prepared(java_project, jdks, maven, gradle, tmp_path)
    try:
        with owner.use() as context:
            adapter = lb.create("java", context)
            copy = Path(context.root)
            request = SuiteRunRequest(suite=_suite(copy), timeout_s=600,
                                      remove_created_files=remove)
            first = adapter.run_suite(request)
            assert first.outcome is lb.Outcome.COMPLETED, first.output
            assert first.side_effects.files_removed == ()
            assert (copy / "made.txt").is_file()
            (copy / "between.txt").write_text("the caller's", encoding="utf-8")
            second = adapter.run_suite(request)
            if remove:
                # Only the test's file goes, build output and the caller's stay.
                assert second.outcome is lb.Outcome.COMPLETED, second.output
                assert second.side_effects.files_removed == ("made.txt",)
            else:
                assert second.outcome is lb.Outcome.TEST_FAILURES, second.output
                assert second.side_effects.files_removed == ()
            assert (copy / "between.txt").read_text(encoding="utf-8") == "the caller's"
        assert not (project / "made.txt").exists()
    finally:
        owner.close()


def test_a_run_as_delivered_reports_the_files_it_removed(java_project, jdks, maven, gradle, tmp_path):
    owner, _project = _prepared(java_project, jdks, maven, gradle, tmp_path)
    try:
        with owner.use() as context:
            adapter = lb.create("java", context)
            request = SuiteRunRequest(suite=_suite(Path(context.root)), timeout_s=600,
                                      remove_created_files=True)
            assert adapter.run_suite(request).outcome is lb.Outcome.COMPLETED
            delivered = adapter.tests.run_as_delivered(request)
            assert delivered.outcome is lb.Outcome.COMPLETED, delivered.output
            assert delivered.side_effects.files_removed == ("made.txt",)
    finally:
        owner.close()


def test_recording_a_runs_files_is_no_part_of_its_time(
        java_project, jdks, maven, gradle, tmp_path, monkeypatch):
    """Recording a run's created files happens after its launcher ends,
    outside the run's time."""
    owner, _project = _prepared(java_project, jdks, maven, gradle, tmp_path)
    try:
        with owner.use() as context:
            adapter = lb.create("java", context)
            request = SuiteRunRequest(suite=_suite(Path(context.root)), timeout_s=600,
                                      remove_created_files=True)
            plain = adapter.run_suite(request)
            assert plain.outcome is lb.Outcome.COMPLETED, plain.output
            # Longer than the run, so recording inside its time would show.
            delay = max(3.0, 3 * plain.wall_time_s)
            record = CreatedFiles.end

            def slow(self, *args, **kwargs):
                time.sleep(delay)
                return record(self, *args, **kwargs)

            monkeypatch.setattr(CreatedFiles, "end", slow)
            slowed = adapter.run_suite(request)
            assert slowed.outcome is lb.Outcome.COMPLETED, slowed.output
            assert slowed.side_effects.files_removed == ("made.txt",)
            assert slowed.wall_time_s < delay
    finally:
        owner.close()


def test_a_timed_out_benchmark_records_the_files_of_its_stopped_run(
        java_project, jdks, maven, gradle, tmp_path):
    """A file written by a JVM that a repetition's timeout stopped is removed
    by the next run that asks for it."""
    owner, _project = _prepared(java_project, jdks, maven, gradle, tmp_path)
    try:
        with owner.use() as context:
            adapter = lb.create("java", context)
            copy = Path(context.root)
            (copy / "src" / "test" / "java" / "demo" / "HangsTest.java").write_text(
                HANGS, encoding="utf-8")
            hangs = lb.Suite(str(copy / "src" / "test" / "java"), ("demo/HangsTest.java",))
            measured = adapter.benchmark(BenchmarkRequest(
                hangs, 1, 20, warmup_runs=0, remove_created_files=True))
            assert measured.outcome is lb.Outcome.TIMED_OUT, measured.reason
            assert (copy / "made.txt").is_file()
            after = adapter.run_suite(SuiteRunRequest(
                suite=_suite(copy), timeout_s=600, remove_created_files=True))
            assert after.side_effects.files_removed == ("made.txt",)
            assert after.outcome is lb.Outcome.COMPLETED, after.output
    finally:
        owner.close()


@pytest.mark.parametrize("operation", ["per_test_evidence", "benchmark"])
def test_a_java_measurement_removes_and_records_the_files_of_its_runs(
        java_project, jdks, maven, gradle, tmp_path, operation):
    """A per-test measurement (without its warm-up run, whose file the
    measured run would find) and a benchmark remove what the run before
    them created, and the run after them removes what they created."""
    owner, _project = _prepared(java_project, jdks, maven, gradle, tmp_path)
    try:
        with owner.use() as context:
            adapter = lb.create("java", context)
            suite = _suite(Path(context.root))
            run = SuiteRunRequest(suite=suite, timeout_s=600, remove_created_files=True)
            assert adapter.run_suite(run).outcome is lb.Outcome.COMPLETED
            if operation == "per_test_evidence":
                measured = adapter.per_test_evidence(PerTestEvidenceRequest(
                    suite=suite, target="src/main/java/demo/Calc.java", timeout_s=600,
                    warmup=False, remove_created_files=True))
            else:
                measured = adapter.benchmark(BenchmarkRequest(
                    suite, 1, 600, warmup_runs=0, remove_created_files=True))
            assert measured.outcome is lb.Outcome.COMPLETED, measured.reason
            assert measured.side_effects.files_removed == ("made.txt",)
            after = adapter.run_suite(run)
            assert after.outcome is lb.Outcome.COMPLETED, after.output
            assert after.side_effects.files_removed == ("made.txt",)
    finally:
        owner.close()
