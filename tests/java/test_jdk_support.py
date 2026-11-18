"""The setup check and every Java operation refuse a JDK below 11 before
anything starts. An untested JDK gets a note, a tested one none."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.runs import (
    BenchmarkRequest, CollectionRequest, PerTestEvidenceRequest, SuiteRunRequest,
    WarmedSuiteBenchmarkRequest,
)

sys.path.insert(0, os.path.dirname(__file__))
from conftest import write_project

REFUSAL = ("JDK 1.8.0_412 is not supported: Java operations need JDK 11 or newer "
           "(tested 11, 17, 21, 25, 26, 27), since PIT 1.30 and the helpers, compiled for "
           "release 11, need JDK 11")


def _jdk(tmp_path, version):
    home = tmp_path / f"jdk-{version}"
    (home / "bin").mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for tool in ("java", "javac"):
        (home / "bin" / f"{tool}{suffix}").write_text("", encoding="utf-8")
    (home / "release").write_text(f'JAVA_VERSION="{version}"\n', encoding="utf-8")
    return str(home / "bin" / f"java{suffix}")


def _check(tmp_path, version):
    project = write_project(tmp_path / "calc", "maven", 11, "junit5")
    context = lb.ProjectContext(str(project), lb.Interpreter(path=_jdk(tmp_path, version)))
    report = lb.inspect_project("java", lb.ProjectSetupRequest(
        context, ("src/main/java/demo/Calc.java",), ("tests.run_suite",)))
    return report, next(item for item in report.items if item.key == "runtime")


def test_a_jdk_below_11_is_unsupported_in_the_setup_check(tmp_path):
    report, runtime = _check(tmp_path, "1.8.0_412")
    assert runtime.outcome is lb.Outcome.UNSUPPORTED and runtime.reason == REFUSAL
    assert runtime.version is None and runtime.action == "select a JDK 11 or newer"
    assert report.outcome is lb.Outcome.UNSUPPORTED
    run = next(item for item in report.operations if item.name == "tests.run_suite")
    assert run.readiness is lb.Availability.UNSUPPORTED and run.reason == REFUSAL


def test_an_untested_jdk_runs_with_a_note(tmp_path):
    _, expected = _check(tmp_path, "19.0.2")
    assert expected.outcome is lb.Outcome.COMPLETED and expected.version == "19.0.2"
    assert expected.reason.endswith(
        "JDK 19.0.2 is expected to work but is not among the tested JDKs (11, 17, 21, 25, 26, 27)")
    _, newer = _check(tmp_path / "newer", "28")
    assert newer.reason.endswith("JDK 28 is not among the tested JDKs (11, 17, 21, 25, 26, 27)")


def test_a_tested_jdk_gets_no_note(tmp_path):
    _, runtime = _check(tmp_path, "21.0.11")
    assert runtime.version == "21.0.11"
    assert runtime.reason == "JDK found, and the project is not compiled or run yet"


def test_every_operation_refuses_a_jdk_below_11_before_anything_starts(tmp_path, monkeypatch):
    def started(*args, **kwargs):
        pytest.fail("a command started for an unsupported JDK")

    project = write_project(tmp_path / "calc", "maven", 11, "junit5")
    adapter = lb.create("java", lb.ProjectContext(str(project), lb.Interpreter(path=_jdk(tmp_path, "1.8.0_412"))))
    monkeypatch.setattr(subprocess, "Popen", started)
    suite = lb.Suite(str(project / "src" / "test" / "java"), ("demo/CalcTest.java",))
    target = "src/main/java/demo/Calc.java"
    results = [
        adapter.tests.run_suite(SuiteRunRequest(suite, 60)),
        adapter.tests.collect_suite(CollectionRequest(suite, 60)),
        adapter.tests.run_as_delivered(SuiteRunRequest(suite, 60)),
        adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, target, 60)),
        adapter.tests.benchmark(BenchmarkRequest(suite, 1, 60)),
        adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(suite, "native_lifecycle", 60, 120)),
    ]
    for result in results:
        assert result.outcome is lb.Outcome.UNSUPPORTED and REFUSAL in result.reason, result
    with adapter.mutations.open_session("pit") as session:
        assert session.outcome is lb.Outcome.UNSUPPORTED and session.reason == REFUSAL


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation adds .exe")
def test_a_java_path_without_exe_is_read_as_its_jdk(tmp_path):
    """On Windows a JDK's java given without .exe, as its start finds it,
    reads as that JDK's home. A path that names no file is no JDK."""
    from langadapter.java._env import read

    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    for name in ("java.exe", "javac.exe"):
        (home / "bin" / name).write_text("", encoding="ascii")
    (home / "release").write_text('JAVA_VERSION="21.0.2"\n', encoding="ascii")
    jdk = read(str(home / "bin" / "java"))
    assert jdk is not None and jdk.home == str(home) and jdk.feature == 21
    assert read(str(home / "bin" / "absent")) is None
